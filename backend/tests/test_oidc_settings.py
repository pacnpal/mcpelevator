"""SSO settings (#267): one OpenID Connect provider configured through PATCH /api/settings
or MCPE_OIDC_* env vars. Discovery is fetched from a fake identity provider behind
httpx.MockTransport."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from conftest import LOOPBACK, mint_token as _mint

from app.auth import oidc
from app.db import get_engine, repo
from app.db.models import Setting, Token, User
from app.main import app
from app.registry import settings as runtime_settings

ISSUER = "https://idp.example.com"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
}
_OIDC_KEYS = [k for k in runtime_settings.DEFAULTS if k.startswith("oidc_")]


def _idp(doc: dict, seen: list[str] | None = None) -> httpx.MockTransport:
    """A fake identity provider serving ``doc`` at its well-known discovery path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=doc)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clean():
    yield
    with Session(get_engine()) as s:
        for key in _OIDC_KEYS:
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        s.commit()
        runtime_settings.write(s, {"control_plane_auth": "auto", "bind_mode": "local"})
        s.execute(delete(Token))
        s.execute(delete(User))
        s.commit()


def test_invalid_identity_claim_is_refused():
    with TestClient(app) as client:
        r = client.patch("/api/settings", json={"oidc_identity_claim": "nickname"}, headers=LOOPBACK)
        assert r.status_code == 400
        assert "oidc_identity_claim" in r.json()["detail"]
        assert client.get("/api/settings", headers=LOOPBACK).json()["oidc_identity_claim"] == "email"


def test_enabling_with_discovery_missing_jwks_uri_names_it(monkeypatch):
    doc = {k: v for k, v in DISCOVERY.items() if k != "jwks_uri"}
    monkeypatch.setattr(oidc, "TRANSPORT", _idp(doc))
    with TestClient(app) as client:
        r = client.patch(
            "/api/settings",
            json={"oidc_enabled": True, "oidc_discovery_url": ISSUER, "oidc_client_id": "c"},
            headers=LOOPBACK,
        )
        assert r.status_code == 400
        assert "jwks_uri" in r.json()["detail"]
        assert client.get("/api/settings", headers=LOOPBACK).json()["oidc_enabled"] is False


def test_enabling_with_unreachable_discovery_is_refused(monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(oidc, "TRANSPORT", httpx.MockTransport(down))
    with TestClient(app) as client:
        r = client.patch(
            "/api/settings", json={"oidc_enabled": True, "oidc_discovery_url": ISSUER},
            headers=LOOPBACK,
        )
        assert r.status_code == 400
        assert "discovery" in r.json()["detail"]


def test_bare_issuer_is_accepted(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(oidc, "TRANSPORT", _idp(DISCOVERY, seen))
    with TestClient(app) as client:
        r = client.patch(
            "/api/settings",
            json={"oidc_enabled": True, "oidc_discovery_url": ISSUER, "oidc_client_id": "c"},
            headers=LOOPBACK,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["oidc_enabled"] is True
        assert body["oidc_discovery_url"] == ISSUER
        assert seen == [f"{ISSUER}/.well-known/openid-configuration"]
        assert body["oidc_redirect_uri"].endswith("/api/auth/oidc/callback")


def test_secret_is_masked_for_admin_and_member():
    with TestClient(app) as client:
        r = client.patch("/api/settings", json={"oidc_client_secret": "s3cret"}, headers=LOOPBACK)
        assert r.status_code == 200
        assert r.json()["oidc_client_secret"] == "<hidden>"
        admin = {**LOOPBACK, "authorization": f"Bearer {_mint('control')}"}
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": "always"})
            assert repo.setting_get(s, "oidc_client_secret", "") != "s3cret"  # sealed at rest
        user = client.post("/api/users", json={"name": "m", "role": "member"}, headers=admin).json()
        cred = client.post(f"/api/users/{user['id']}/credentials", headers=admin).json()["token"]
        member = {**LOOPBACK, "authorization": f"Bearer {cred}"}
        for headers in (admin, member):
            got = client.get("/api/settings", headers=headers)
            assert got.status_code == 200
            assert got.json()["oidc_client_secret"] == "<hidden>"
            assert "s3cret" not in got.text


def test_hidden_round_trip_keeps_secret_and_empty_clears_it():
    with TestClient(app) as client:
        client.patch("/api/settings", json={"oidc_client_secret": "s3cret"}, headers=LOOPBACK)
        r = client.patch(
            "/api/settings",
            json={"oidc_client_secret": "<hidden>", "oidc_label": "Company SSO"},
            headers=LOOPBACK,
        )
        assert r.status_code == 200
        with Session(get_engine()) as s:
            assert runtime_settings.oidc_client_secret(s) == "s3cret"
        client.patch("/api/settings", json={"oidc_client_secret": ""}, headers=LOOPBACK)
        assert client.get("/api/settings", headers=LOOPBACK).json()["oidc_client_secret"] == ""
        with Session(get_engine()) as s:
            assert runtime_settings.oidc_client_secret(s) == ""


def test_env_seeds_once_and_a_later_ui_write_survives_restart(monkeypatch):
    from app import main

    env = SimpleNamespace(
        oidc_enabled=False, oidc_discovery_url=ISSUER, oidc_client_id="from-env",
        oidc_client_secret="", oidc_label="", oidc_scopes="groups, offline_access",
        oidc_identity_claim="preferred_username", oidc_allowed_tenants="",
        oidc_require_verified_email=None,
    )
    monkeypatch.setattr(main, "get_settings", lambda: env)
    main._bootstrap_oidc()
    with Session(get_engine()) as s:
        values = runtime_settings.read_all(s)
        assert values["oidc_client_id"] == "from-env"
        assert values["oidc_scopes"] == ["groups", "offline_access"]
        assert values["oidc_identity_claim"] == "preferred_username"
        runtime_settings.write(s, {"oidc_client_id": "from-ui"})
    main._bootstrap_oidc()  # restart: already written, so the env is ignored
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_client_id"] == "from-ui"


def test_env_client_secret_wins_over_stored(monkeypatch):
    from app import config

    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oidc_client_secret": "stored"})
        monkeypatch.setenv("MCPE_OIDC_CLIENT_SECRET", "from-env")
        config.get_settings.cache_clear()
        try:
            assert runtime_settings.oidc_client_secret(s) == "from-env"
        finally:
            monkeypatch.delenv("MCPE_OIDC_CLIENT_SECRET")
            config.get_settings.cache_clear()
        assert runtime_settings.oidc_client_secret(s) == "stored"


_ENTRA_DOC = {**DISCOVERY, "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"}
_TENANT = "9188040D-6C67-4C5B-B112-36A304B66DAD"


def test_enabling_multi_tenant_without_a_tenant_list_is_refused(monkeypatch):
    monkeypatch.setattr(oidc, "TRANSPORT", _idp(_ENTRA_DOC))
    enable = {"oidc_enabled": True, "oidc_discovery_url": ISSUER, "oidc_client_id": "c"}
    with TestClient(app) as client:
        r = client.patch("/api/settings", json=enable, headers=LOOPBACK)
        assert r.status_code == 400
        assert "oidc_allowed_tenants" in r.json()["detail"]
        assert client.get("/api/settings", headers=LOOPBACK).json()["oidc_enabled"] is False
        r = client.patch(
            "/api/settings",
            json={**enable, "oidc_allowed_tenants": [_TENANT, _TENANT.lower()]},
            headers=LOOPBACK,
        )
        assert r.status_code == 200, r.text
        assert r.json()["oidc_allowed_tenants"] == [_TENANT.lower()]


def test_a_tenant_that_is_not_a_guid_is_refused():
    with TestClient(app) as client:
        r = client.patch(
            "/api/settings", json={"oidc_allowed_tenants": ["contoso"]}, headers=LOOPBACK
        )
        assert r.status_code == 400
        assert "oidc_allowed_tenants" in r.json()["detail"]


def test_verified_email_is_required_by_default_and_env_false_seeds_off(monkeypatch):
    from app import main

    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_require_verified_email"] is True
    env = SimpleNamespace(
        oidc_enabled=False, oidc_discovery_url="", oidc_client_id="", oidc_label="",
        oidc_scopes="", oidc_identity_claim="", oidc_allowed_tenants="",
        oidc_require_verified_email=False,
    )
    monkeypatch.setattr(main, "get_settings", lambda: env)
    main._bootstrap_oidc()
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_require_verified_email"] is False
