"""Sign-in session refresh against the provider (#273): a session soft-expires with the
provider's access token and only ``/api/auth/status`` refreshes it, under a hard cap
(``oidc_session_max_age_s``). The fake identity provider rotates single-use refresh
tokens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import LOOPBACK

# idp is the fixture; the rest are the sign-in helpers.
from test_sso_sign_in import (
    GOOGLE,
    _enforce,
    _session_headers,
    _sessions,
    _settings,
    _sign_in,
    _start,
    _user,
    idp,
)

from app import main
from app.db import get_engine
from app.db.models import Setting, SignInSession
from app.main import app
from app.registry import settings as runtime_settings


def _age(*, soft: bool = False, hard: bool = False) -> None:
    """Move every session past its soft expiry, its hard expiry, or both."""
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with Session(get_engine()) as s:
        for row in s.exec(select(SignInSession)).all():
            if soft:
                row.soft_expires_at = past
            if hard:
                row.expires_at = past
            s.add(row)
        s.commit()


def _status(client: TestClient) -> dict:
    return client.get("/api/auth/status", headers=LOOPBACK).json()


def _signed_in_ada(client: TestClient, idp, **claims) -> dict[str, str]:
    idp.claims = {"email": "ada@example.com", **claims}
    admin = _enforce()
    _sign_in(client, idp)
    return admin


def test_a_soft_expired_session_gets_401_until_status_refreshes_it(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        headers = _session_headers(client)
        _age(soft=True)
        assert client.get("/api/servers", headers=headers).status_code == 401
        assert _status(client)["authenticated"] is True
        assert client.get("/api/servers", headers=headers).status_code == 200
    assert len(idp.refreshes) == 1


def test_each_refresh_sends_the_newest_refresh_token_once(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        for _ in range(3):
            _age(soft=True)
            assert _status(client)["authenticated"] is True
    assert len(idp.refreshes) == 3 and len(set(idp.refreshes)) == 3


def test_removal_from_the_admin_group_takes_effect_at_refresh(idp):
    _user("ada@example.com")
    _settings(oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        _signed_in_ada(client, idp, groups=["admins"])
        headers = _session_headers(client)
        assert client.get("/api/users", headers=headers).status_code == 200
        idp.claims["groups"] = ["staff"]  # the provider's admin removes her from the group
        _age(soft=True)
        status = _status(client)
        assert status["authenticated"] is True and status["user"]["role"] == "member"
        assert client.get("/api/users", headers=headers).status_code == 403


def test_invalid_grant_ends_the_session(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        idp.token_error = "invalid_grant"
        _age(soft=True)
        assert _status(client)["authenticated"] is False
    assert _sessions() == 0


def test_a_refreshed_id_token_for_another_subject_ends_the_session(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        idp.claims["sub"] = "sub-2"
        _age(soft=True)
        assert _status(client)["authenticated"] is False
    assert _sessions() == 0


def test_a_provider_outage_keeps_the_session_until_hard_expiry(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        headers = _session_headers(client)
        idp.down = True
        _age(soft=True)
        assert _status(client)["authenticated"] is True
        assert client.get("/api/servers", headers=headers).status_code == 200
        assert _status(client)["authenticated"] is True  # no second try within the minute
        assert len(idp.refreshes) == 0 and idp.seen.count("/token") == 2
        _age(hard=True)
        assert client.get("/api/servers", headers=headers).status_code == 401
        assert _status(client)["authenticated"] is False


def test_offline_access_is_requested_only_when_advertised(idp):
    with TestClient(app) as client:
        idp.authorize(_start(client).headers["location"])
        assert "offline_access" not in idp.last_authorize["scope"].split()
        idp.scopes_supported = ["openid", "email", "offline_access"]
        idp.authorize(_start(client).headers["location"])
        assert "offline_access" in idp.last_authorize["scope"].split()
        assert "access_type" not in idp.last_authorize


def test_google_gets_access_type_offline_instead(idp):
    idp.discovery_issuer = GOOGLE
    idp.scopes_supported = ["openid", "email", "profile"]
    with TestClient(app) as client:
        idp.authorize(_start(client).headers["location"])
    assert idp.last_authorize["access_type"] == "offline"
    assert "offline_access" not in idp.last_authorize["scope"].split()


def test_without_a_refresh_token_the_session_lasts_to_hard_expiry(idp):
    _user("ada@example.com")
    idp.refresh_tokens = False
    idp.expires_in = 0  # the access token is spent at once
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        headers = _session_headers(client)
        assert client.get("/api/servers", headers=headers).status_code == 200
        _age(hard=True)
        assert client.get("/api/servers", headers=headers).status_code == 401
    assert idp.refreshes == []


def test_a_sign_in_settings_change_ends_the_session_at_its_next_refresh(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp, email_verified=True)
        headers = _session_headers(client)
        _settings(oidc_allowed_domains=["example.com"], oidc_label="Acme")
        assert client.get("/api/servers", headers=headers).status_code == 200
        _age(soft=True)
        assert _status(client)["authenticated"] is False
    assert _sessions() == 0


def test_the_hard_cap_is_a_validated_setting_seeded_once(idp, monkeypatch):
    _user("ada@example.com")
    with TestClient(app) as client:
        admin = _enforce()
        assert client.get("/api/settings", headers=admin).json()["oidc_session_max_age_s"] == 43200
        for bad in (299, True, "600"):
            r = client.patch("/api/settings", headers=admin, json={"oidc_session_max_age_s": bad})
            assert r.status_code in (400, 422), bad
        r = client.patch("/api/settings", headers=admin, json={"oidc_session_max_age_s": 600})
        assert r.status_code == 200
        idp.claims = {"email": "ada@example.com"}
        cookie = next(
            h for h in _sign_in(client, idp).headers.get_list("set-cookie")
            if h.startswith("mcpe_session=")
        )
        assert "max-age=600" in cookie.lower()
    with Session(get_engine()) as s:
        row = s.exec(select(SignInSession)).one()
        assert row.expires_at - row.created_at == timedelta(seconds=600)
        s.delete(s.get(Setting, "oidc_session_max_age_s"))  # back to a first boot
        s.commit()
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(oidc_session_max_age_s=900))
    main._bootstrap_oidc_session_max_age()
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_session_max_age_s"] == 900
        runtime_settings.write(s, {"oidc_session_max_age_s": 1200})
    main._bootstrap_oidc_session_max_age()  # restart: already written, so the env is ignored
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_session_max_age_s"] == 1200
