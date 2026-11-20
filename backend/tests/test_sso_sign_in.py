"""Signing in with SSO as an existing User (#268): authorization code + PKCE against a
fake identity provider, the Sign-in session cookie and its CSRF header, and where that
session counts (only /api, never /s, /g or /api/mcp)."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select
from starlette.responses import JSONResponse

from conftest import LOOPBACK, create_server, mint_token
from fake_idp import CLIENT_ID, ISSUER, FakeIdP

from app.auth import oidc, sign_in
from app.db import get_engine, repo
from app.db.models import Server, Setting, SignInSession, Token, User
from app.main import app
from app.registry import settings as runtime_settings
from app.registry import service
from app.supervisor.supervisor import Supervisor
from app.util import new_id

_OIDC_KEYS = [k for k in runtime_settings.DEFAULTS if k.startswith("oidc_")]


async def _parked(self):  # keep the reconciler from dialing bridges TestClient never binds
    import asyncio

    await asyncio.Event().wait()


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    monkeypatch.setattr(oidc, "TRANSPORT", fake.transport)
    monkeypatch.setattr(Supervisor, "run_forever", _parked)
    with Session(get_engine()) as s:
        runtime_settings.write(s, {
            "oidc_enabled": True, "oidc_discovery_url": ISSUER, "oidc_client_id": CLIENT_ID,
            "oidc_label": "Acme ID",
        })
    yield fake
    with Session(get_engine()) as s:
        for key in _OIDC_KEYS:
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        s.commit()
        runtime_settings.write(s, {
            "control_plane_auth": "auto", "bind_mode": "local", "groups": {},
            "default_auth_provider": "none",
        })
        for model in (SignInSession, Token, User, Server):
            s.execute(delete(model))
        s.commit()


def _user(name: str, role: str = "member") -> str:
    with Session(get_engine()) as s:
        return repo.create_user(s, User(id=new_id(), name=name, role=role)).id


def _enforce() -> dict[str, str]:
    """Enforcement on; returns admin bearer headers (a legacy user-less control token)."""
    admin = mint_token("control")
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    return {**LOOPBACK, "authorization": f"Bearer {admin}"}


def _start(client: TestClient, next_path: str = "/servers") -> httpx.Response:
    r = client.get(
        "/api/auth/oidc/login", params={"next": next_path}, headers=LOOPBACK, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    return r


def _callback(client: TestClient, query: dict) -> httpx.Response:
    return client.get(
        "/api/auth/oidc/callback", params=query, headers=LOOPBACK, follow_redirects=False
    )


def _sign_in(client: TestClient, idp: FakeIdP, next_path: str = "/servers") -> httpx.Response:
    return _callback(client, idp.authorize(_start(client, next_path).headers["location"]))


def _reason(r: httpx.Response) -> str | None:
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert url.path == "/login"
    return parse_qs(url.query).get("reason", [None])[0]


def _csrf(client: TestClient) -> str:
    csrf = client.get("/api/auth/status", headers=LOOPBACK).json()["csrf"]
    assert csrf
    return csrf


def _session_headers(client: TestClient) -> dict[str, str]:
    return {**LOOPBACK, "x-mcpe-csrf": _csrf(client)}


# ---- the flow -----------------------------------------------------------------


def test_sign_in_sets_the_cookie_and_lands_on_next(idp):
    _user("Ada@Example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        r = _sign_in(client, idp, "/server/abc?tab=logs")
        assert r.status_code == 303
        assert r.headers["location"] == "/server/abc?tab=logs"
        cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("mcpe_session="))
        low = cookie.lower()
        assert "httponly" in low and "samesite=strict" in low and "path=/" in low

        status = client.get("/api/auth/status", headers=LOOPBACK).json()
        assert status["authenticated"] is True
        assert status["user"]["name"] == "Ada@Example.com"
        assert status["user"]["linked"] is True
        assert status["sso"] == {"enabled": True, "label": "Acme ID"}
        assert status["csrf"]
        assert client.get("/api/servers", headers=_session_headers(client)).status_code == 200


def test_an_https_base_gets_the_host_prefixed_secure_cookie(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    https = {**LOOPBACK, "x-forwarded-proto": "https"}
    with TestClient(app) as client:
        start = client.get("/api/auth/oidc/login", headers=https, follow_redirects=False)
        query = idp.authorize(start.headers["location"])
        assert "https://127.0.0.1/api/auth/oidc/callback" in start.headers["location"].replace("%3A", ":").replace("%2F", "/")
        # The flow cookie is Secure here, so the test client won't send it back over http:
        # hand it over explicitly, as the browser would over https.
        flow = next(h for h in start.headers.get_list("set-cookie") if h.startswith("mcpe_oidc_"))
        name, _, rest = flow.partition("=")
        r = client.get(
            "/api/auth/oidc/callback", params=query,
            headers={**https, "cookie": f"{name}={rest.split(';')[0]}"}, follow_redirects=False,
        )
        cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("__Host-mcpe_session="))
        assert "secure" in cookie.lower()


def test_next_must_be_a_same_origin_path(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        for bad in ("https://evil.example/", "//evil.example", "/\\evil.example"):
            assert _sign_in(client, idp, bad).headers["location"] == "/"


def test_login_on_a_non_canonical_host_goes_to_the_public_base_first(idp, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "public_base_url", "https://mcp.example.com")
    with TestClient(app) as client:
        r = client.get(
            "/api/auth/oidc/login", params={"next": "/users"}, headers=LOOPBACK,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "https://mcp.example.com/api/auth/oidc/login?next=%2Fusers"


def test_two_tabs_signing_in_at_once_both_finish(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        first = idp.authorize(_start(client, "/a").headers["location"])
        second = idp.authorize(_start(client, "/b").headers["location"])
        assert _callback(client, second).headers["location"] == "/b"
        assert _callback(client, first).headers["location"] == "/a"


def test_unknown_state_is_expired(idp):
    with TestClient(app) as client:
        assert _reason(_callback(client, {"code": "x", "state": "nope"})) == "expired"


def test_sso_off_refuses_login(idp):
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oidc_enabled": False})
    with TestClient(app) as client:
        r = client.get("/api/auth/oidc/login", headers=LOOPBACK, follow_redirects=False)
        assert _reason(r) == "sso_disabled"
        assert client.get("/api/auth/status", headers=LOOPBACK).json()["sso"] is None


def test_provider_error_maps_to_our_code(idp):
    with TestClient(app) as client:
        state = parse_qs(urlsplit(_start(client).headers["location"]).query)["state"][0]
        r = _callback(client, {"error": "access_denied", "state": state,
                               "error_description": "<script>"})
        assert _reason(r) == "provider_denied"
        assert "script" not in r.headers["location"]


@pytest.mark.parametrize(
    "spoil",
    ["bad_nonce", "wrong_audience", "hs256", "userinfo_sub", "wrong_iss_param", "expired_token"],
)
def test_an_id_token_that_fails_a_check_is_refused(idp, spoil):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        start = _start(client)
        query = idp.authorize(start.headers["location"])
        nonce = parse_qs(urlsplit(start.headers["location"]).query)["nonce"][0]
        if spoil == "bad_nonce":
            idp.id_token_override = idp.id_token("someone-elses-nonce")
        elif spoil == "wrong_audience":
            idp.id_token_override = idp.id_token(nonce, aud="another-client")
        elif spoil == "hs256":
            claims = jwt_claims(idp, nonce)
            idp.id_token_override = idp.sign(claims, alg="HS256", key="a-shared-secret-of-32-bytes!!!!!")
        elif spoil == "userinfo_sub":
            idp.userinfo = {"sub": "someone-else", "email": "ada@example.com"}
        elif spoil == "wrong_iss_param":
            query["iss"] = "https://other-idp.example.com"
        elif spoil == "expired_token":
            idp.id_token_override = idp.id_token(nonce, exp=1, iat=0)
        r = _callback(client, query)
        assert _reason(r) == "provider_rejected"
        assert not any("mcpe_session" in h for h in r.headers.get_list("set-cookie"))


def jwt_claims(idp: FakeIdP, nonce: str) -> dict:
    import time

    now = int(time.time())
    return {"iss": idp.issuer, "aud": idp.client_id, "sub": "sub-1", "iat": now,
            "exp": now + 300, "nonce": nonce, **idp.claims}


def test_matching_userinfo_is_merged(idp):
    _user("ada")
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oidc_identity_claim": "preferred_username"})
    idp.userinfo = {"sub": "sub-1", "preferred_username": "ada"}
    with TestClient(app) as client:
        assert _sign_in(client, idp).headers["location"] == "/servers"


def test_no_user_with_that_name_is_no_match(idp):
    idp.claims = {"email": "nobody@example.com"}
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "no_match"


def test_two_users_with_the_same_name_is_ambiguous(idp):
    _user("ada@example.com")
    _user("ADA@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "ambiguous_match"


# ---- where the session counts ---------------------------------------------------


def test_the_cookie_without_the_csrf_header_is_refused(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        _sign_in(client, idp)
        assert client.get("/api/servers", headers=LOOPBACK).status_code == 401
        wrong = {**LOOPBACK, "x-mcpe-csrf": "0" * 64}
        assert client.get("/api/servers", headers=wrong).status_code == 401


def test_a_wrong_origin_is_refused_by_the_host_guard(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        _sign_in(client, idp)
        headers = {**_session_headers(client), "origin": "https://evil.example"}
        assert client.get("/api/servers", headers=headers).status_code == 403


def test_the_cookie_alone_opens_nothing_on_the_data_plane_or_the_control_mcp(idp):
    _user("ada@example.com", role="admin")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        admin = _enforce()
        with Session(get_engine()) as s:
            service.ensure_builtin(s)
            runtime_settings.write(s, {"default_auth_provider": "bearer", "groups": {"team": "*"}})
        srv = client.post(
            "/api/servers", json={"name": "px", "command": "echo", "auth_provider": "bearer"},
            headers=admin,
        ).json()
        _sign_in(client, idp)
        headers = {**_session_headers(client), "accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        for path in (f"/s/{srv['slug']}/mcp", "/g/team/mcp", "/api/mcp"):
            assert client.post(path, json=body, headers=headers).status_code == 401, path


def test_an_expired_session_is_refused(idp, monkeypatch):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    monkeypatch.setattr(sign_in, "MAX_AGE", timedelta(seconds=-1))
    with TestClient(app) as client:
        _enforce()
        _sign_in(client, idp)
        status = client.get("/api/auth/status", headers=LOOPBACK).json()
        assert status["authenticated"] is False and status["csrf"] is None


def test_a_bearer_header_is_judged_alone(idp):
    _user("ada@example.com", role="admin")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        _sign_in(client, idp)
        headers = {**_session_headers(client), "authorization": "Bearer not-a-token"}
        assert client.get("/api/servers", headers=headers).status_code == 401
        member = mint_token("all")  # a valid token, but not a login
        headers["authorization"] = f"Bearer {member}"
        assert client.get("/api/servers", headers=headers).status_code == 403


# ---- when the session ends ------------------------------------------------------


def test_logout_ends_the_session(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        _sign_in(client, idp)
        headers = _session_headers(client)
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/servers", headers=headers).status_code == 401


def test_changing_the_client_id_ends_sessions(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        _sign_in(client, idp)
        headers = _session_headers(client)
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"oidc_label": "Renamed"})  # not an ending key
        assert client.get("/api/servers", headers=headers).status_code == 200
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"oidc_client_id": "another"})
        assert client.get("/api/servers", headers=headers).status_code == 401


def test_deleting_the_user_ends_the_session(idp):
    uid = _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        admin = _enforce()
        _sign_in(client, idp)
        headers = _session_headers(client)
        assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 204
        assert client.get("/api/servers", headers=headers).status_code == 401


def test_a_demotion_applies_on_the_next_request(idp):
    uid = _user("ada@example.com", role="admin")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        admin = _enforce()
        _sign_in(client, idp)
        headers = _session_headers(client)
        assert client.get("/api/users", headers=headers).status_code == 200
        r = client.patch(f"/api/users/{uid}", json={"role": "member"}, headers=admin)
        assert r.status_code == 200
        assert client.get("/api/users", headers=headers).status_code == 403


# ---- guards and the built-in server ---------------------------------------------


def test_an_admin_session_passes_the_lock_out_guard_but_is_no_admin_credential(idp):
    _user("ada@example.com", role="admin")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _sign_in(client, idp)  # enforcement is off here
        r = client.patch(
            "/api/settings", json={"control_plane_auth": "always"}, headers=_session_headers(client)
        )
        assert r.status_code == 200, r.text
        with Session(get_engine()) as s:
            assert repo.admin_credential_exists(s) is False


def test_a_member_session_does_not_pass_the_lock_out_guard(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _sign_in(client, idp)
        r = client.patch(
            "/api/settings", json={"control_plane_auth": "always"}, headers=_session_headers(client)
        )
        assert r.status_code == 400


def test_the_playground_refuses_the_built_in_server_to_a_session(idp):
    _user("ada@example.com", role="admin")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        with Session(get_engine()) as s:
            builtin = service.ensure_builtin(s) or service.get_builtin(s)
        _sign_in(client, idp)
        r = client.post(
            f"/api/servers/{builtin.id}/tools/whoami/call", json={}, headers=_session_headers(client)
        )
        assert r.status_code == 400
        assert "login token" in r.json()["detail"]


# ---- the proxies drop the cookie ------------------------------------------------


async def _echo_headers(scope, receive, send):
    await JSONResponse({"headers": [k.decode() for k, _ in scope["headers"]]})(scope, receive, send)


def test_the_s_proxy_and_the_group_hub_drop_the_cookie_and_csrf_header(idp):
    browser = {**LOOPBACK, "cookie": "mcpe_session=abc", "x-mcpe-csrf": "def"}
    with TestClient(app) as client:
        srv = create_server(client, name="px", auth="none")
        client.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_echo_headers))
        client.app.state.supervisor.endpoint = lambda slug: ("backend", 9000)
        seen = client.get(f"/s/{srv['slug']}/mcp", headers=browser).json()["headers"]
        assert "cookie" not in seen and "x-mcpe-csrf" not in seen

        with Session(get_engine()) as s:
            runtime_settings.write(s, {"groups": {"team": "*"}})
        client.app.state.supervisor.on_converged = None
        hub = client.app.state.groups
        prev = hub.app_for
        hub.app_for = lambda n: _echo_headers if n == "team" else prev(n)
        seen = client.post("/g/team/mcp", headers=browser).json()["headers"]
        assert "cookie" not in seen and "x-mcpe-csrf" not in seen


# ---- identity-provider quirks (each one a FakeIdP setting) -----------------------

TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
ENTRA = "https://login.microsoftonline.com/{tenantid}/v2.0"
GOOGLE = "https://accounts.google.com"


def _settings(**values) -> None:
    with Session(get_engine()) as s:
        runtime_settings.write(s, values)


def _linked_issuer(name: str) -> str | None:
    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.name == name)).one().oidc_issuer


def _signed_in(r) -> bool:
    return r.status_code == 303 and r.headers["location"] == "/servers"


# ---- signing algorithms ---------------------------------------------------------


def test_an_eddsa_id_token_signs_in(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    idp.alg = "EdDSA"
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


def test_a_shared_secret_id_token_is_refused_and_says_why(idp, caplog):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    idp.client_secret = "a-shared-secret-of-32-bytes!!!!!"
    _settings(oidc_client_secret=idp.client_secret)
    idp.alg = "HS256"
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="app"):
        assert _reason(_sign_in(client, idp)) == "provider_rejected"
    assert "shared secret" in caplog.text and "signing key" in caplog.text


def test_an_unsigned_id_token_is_refused(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        start = _start(client)
        query = idp.authorize(start.headers["location"])
        nonce = parse_qs(urlsplit(start.headers["location"]).query)["nonce"][0]
        now = int(time.time())
        idp.id_token_override = jwt.encode(
            {"iss": idp.issuer, "aud": idp.client_id, "sub": "sub-1", "iat": now,
             "exp": now + 300, "nonce": nonce, "email": "ada@example.com"},
            None, algorithm="none",
        )
        assert _reason(_callback(client, query)) == "provider_rejected"


# ---- issuers --------------------------------------------------------------------


def _entra(idp, *, tid: str = TENANT, iss_tenant: str = TENANT, key_issuer: str | None = ENTRA):
    idp.discovery_issuer = ENTRA
    idp.jwks_issuer = key_issuer
    idp.claims = {"email": "ada@example.com", "tid": tid,
                  "iss": ENTRA.replace("{tenantid}", iss_tenant)}


def test_entra_multi_tenant_signs_in_and_links_the_concrete_issuer(idp):
    _user("ada@example.com")
    _entra(idp)
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _linked_issuer("ada@example.com") == ENTRA.replace("{tenantid}", TENANT)


@pytest.mark.parametrize(
    "spoil",
    [
        {"tid": "not-a-guid", "iss_tenant": "not-a-guid"},
        {"iss_tenant": "00000000-0000-0000-0000-000000000001"},
        {"key_issuer": "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000001/v2.0"},
    ],
    ids=["tid_not_a_guid", "iss_names_another_tenant", "key_issuer_differs"],
)
def test_entra_refuses_a_token_whose_tenant_does_not_fit(idp, spoil):
    _user("ada@example.com")
    _entra(idp, **spoil)
    idp.iss_param = False
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "provider_rejected"


def test_google_scheme_less_issuer_signs_in_and_links_the_https_form(idp):
    _user("ada@example.com")
    idp.discovery_issuer = GOOGLE
    idp.claims = {"email": "ada@example.com", "iss": "accounts.google.com"}
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _linked_issuer("ada@example.com") == GOOGLE


def test_a_scheme_less_issuer_is_google_only(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com", "iss": "idp.example.com"}
    idp.iss_param = False
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "provider_rejected"


# ---- client authentication -------------------------------------------------------


def test_a_public_client_sends_its_client_id_and_no_secret(idp):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    idp.auth_methods = ["none"]
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


@pytest.mark.parametrize("methods", [["client_secret_post"], ["client_secret_basic"]])
def test_a_confidential_client_uses_the_method_the_provider_lists(idp, methods):
    _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    idp.client_secret = "s3cret"
    idp.auth_methods = methods
    _settings(oidc_client_secret="s3cret")
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


# ---- scopes and claims -----------------------------------------------------------


def test_default_scopes_are_requested_with_the_extras_once(idp):
    _settings(oidc_scopes=["email", "read:things", "openid"])
    with TestClient(app) as client:
        idp.authorize(_start(client).headers["location"])
    assert idp.last_authorize["scope"] == "openid email profile read:things"


def test_a_missing_identity_claim_is_its_own_reason(idp):
    _user("ada@example.com")
    with TestClient(app) as client:
        r = _sign_in(client, idp)
    assert _reason(r) == "missing_claim"
    assert parse_qs(urlsplit(r.headers["location"]).query)["claim"] == ["email"]


def test_an_email_only_in_userinfo_signs_in(idp):
    _user("ada@example.com")
    idp.userinfo = {"sub": "sub-1", "email": "ada@example.com"}
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
