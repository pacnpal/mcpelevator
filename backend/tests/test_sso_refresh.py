"""Sign-in session refresh against the provider (#273): a session soft-expires with the
provider's access token and only ``/api/auth/status`` refreshes it, under a hard cap
(``oidc_session_max_age_s``). The fake identity provider rotates single-use refresh
tokens."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
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
from app.auth import oidc
from app.db import get_engine
from app.db.models import Setting, SignInSession, User
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


def test_a_deactivation_the_refresh_meets_ends_the_session(idp, monkeypatch):
    uid = _user("ada@example.com")
    real = oidc._with_userinfo

    async def userinfo_then_deactivate(*args, **kwargs):
        claims = await real(*args, **kwargs)
        with Session(get_engine()) as s:  # lands while the provider answered
            user = s.get(User, uid)
            user.active = False
            s.add(user)
            s.commit()
        return claims

    with TestClient(app) as client:
        admin = _signed_in_ada(client, idp)
        monkeypatch.setattr(oidc, "_with_userinfo", userinfo_then_deactivate)
        _age(soft=True)
        assert _status(client)["authenticated"] is False
        assert _sessions() == 0
        monkeypatch.setattr(oidc, "_with_userinfo", real)
        r = client.patch(f"/api/users/{uid}", json={"active": True}, headers=admin)
        assert r.status_code == 200
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


def test_a_jwks_outage_after_rotation_keeps_the_new_refresh_token(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        idp.jwks_down = True  # the token endpoint spends the refresh token, then the JWKS 503s
        _age(soft=True)
        assert _status(client)["authenticated"] is True
        idp.jwks_down = False
        _age(soft=True)
        assert _status(client)["authenticated"] is True
    first, second = idp.refreshes
    assert first != second  # the rotated token, not the spent one
    assert _sessions() == 1


def test_a_refreshed_id_token_with_a_bad_signature_ends_the_session(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        now = int(time.time())
        idp.id_token_override = idp.sign(
            {"iss": idp.issuer, "aud": idp.client_id, "sub": "sub-1", "iat": now,
             "exp": now + 300, "email": "ada@example.com"},
            key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        )  # the current kid, another key
        _age(soft=True)
        assert _status(client)["authenticated"] is False
    assert _sessions() == 0


def test_without_a_new_id_token_fresh_userinfo_decides_the_groups(idp):
    _user("ada@example.com")
    _settings(oidc_admin_groups=["admins"])
    idp.userinfo = {"sub": "sub-1"}
    with TestClient(app) as client:
        _signed_in_ada(client, idp, groups=["admins"])
        headers = _session_headers(client)
        assert client.get("/api/users", headers=headers).status_code == 200
        idp.refresh_id_token = False
        idp.userinfo = {"sub": "sub-1", "groups": ["staff"]}  # removed from the admin group
        _age(soft=True)
        status = _status(client)
        assert status["authenticated"] is True and status["user"]["role"] == "member"
        assert client.get("/api/users", headers=headers).status_code == 403


def test_without_fresh_claims_a_group_policy_ends_the_session(idp, caplog):
    _user("ada@example.com")
    _settings(oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        _signed_in_ada(client, idp, groups=["admins"])
        idp.refresh_id_token = False  # and no userinfo endpoint
        _age(soft=True)
        assert _status(client)["authenticated"] is False
    assert _sessions() == 0
    assert "session ended" in caplog.text


def test_without_fresh_claims_and_no_group_policy_the_session_stays(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        _signed_in_ada(client, idp)
        idp.refresh_id_token = False  # and no userinfo endpoint
        _age(soft=True)
        assert _status(client)["authenticated"] is True
    assert _sessions() == 1 and len(idp.refreshes) == 1


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
