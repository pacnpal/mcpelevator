"""One identity across planes (#274): when the ``oauth`` data-plane provider trusts the
SSO issuer, a provider access token (JWT) resolves through the Linked identity to its
User. Its owner (or an admin) then reaches their own ``oauth`` server whatever
``oauth_allowed_subjects`` says, and it drives the control MCP as that User. It never
writes to the User table and never counts as a control credential on the rest of /api."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from fastmcp.server.auth.providers.jwt import JWTVerifier
from sqlmodel import Session, select
from starlette.requests import Request

from conftest import LOOPBACK
from fake_idp import ISSUER, FakeIdP
# idp is the fixture.
from test_sso_sign_in import _echo_headers, _enforce, _unlink, idp

from app.auth import jwks, principal
from app.auth import oauth as oauth_mod
from app.db import get_engine, repo
from app.db.models import Server, Setting, User
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.util import new_id

AUDIENCE = "mcp-api"
MCP = {**LOOPBACK, "accept": "application/json, text/event-stream"}
ENTRA = "https://login.microsoftonline.com/{tenantid}/v2.0"
TENANT = "11111111-2222-3333-4444-555555555555"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"


def _install(fake: FakeIdP, monkeypatch, issuer: str) -> None:
    """Point the ``oauth`` provider at ``fake`` (discovery issuer ``issuer``), with its
    keys in the shared key cache as the startup load leaves them."""
    config_url = runtime_settings.normalize_config_url(ISSUER)
    doc = {"issuer": issuer, "jwks_uri": f"{ISSUER}/jwks"}
    monkeypatch.setattr(oauth_mod, "_discovery_cache", {config_url: (time.monotonic(), doc)})
    kid, private = fake._keys[0]
    public = private.public_key()
    pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    async def verifier_for(config_url: str, audience: str, algorithm: str):
        # The real verifier would fetch the JWKS over the network; this one holds the key.
        return JWTVerifier(public_key=pem, issuer=None, audience=audience, algorithm=algorithm)

    monkeypatch.setattr(oauth_mod, "_verifier_for", verifier_for)
    monkeypatch.setattr(jwks, "_keys", lambda url: SimpleNamespace(
        get_signing_keys=lambda refresh=False: [SimpleNamespace(key_id=kid, key=public)],
        jwk_set_cache=None,
    ))
    monkeypatch.setattr(jwks, "_last_refetch", {})
    monkeypatch.setattr(jwks, "_last_failure", {})
    monkeypatch.setattr(jwks, "_good", {f"{ISSUER}/jwks": ({kid: public}, time.monotonic())})


@pytest.fixture
def plane(idp, monkeypatch):
    _install(idp, monkeypatch, ISSUER)
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oauth_config_url": ISSUER, "oauth_audience": AUDIENCE})
        service.ensure_builtin(s)
    yield idp
    with Session(get_engine()) as s:
        for key in (
            "oauth_config_url", "oauth_audience", "oauth_allowed_subjects", "oauth_member_access",
        ):
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        s.commit()


def _access(fake: FakeIdP, sub: str = "sub-1", **claims) -> dict[str, str]:
    alg = claims.pop("alg", None)
    now = int(time.time())
    token = fake.sign({
        "iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 300, **claims
    }, alg=alg)
    return {"authorization": f"Bearer {token}"}


def _linked(name: str, sub: str, *, role: str = "member", issuer: str = ISSUER) -> str:
    with Session(get_engine()) as s:
        return repo.create_user(s, User(
            id=new_id(), name=name, role=role, oidc_issuer=issuer, oidc_subject=sub,
        )).id


def _oauth_server(client: TestClient, admin: dict, name: str, owner: str | None) -> str:
    r = client.post(
        "/api/servers", json={"name": name, "command": "echo", "auth_provider": "oauth"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    with Session(get_engine()) as s:
        row = s.get(Server, r.json()["id"])
        row.owner_id = owner
        s.add(row)
        s.commit()
    return r.json()["slug"]


def _proxying(client: TestClient) -> None:
    client.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_echo_headers))
    client.app.state.supervisor.endpoint = lambda slug: ("backend", 9000)


def _tool(client: TestClient, headers: dict, name: str, arguments: dict) -> tuple[int, dict]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    r = client.post("/api/mcp", json=body, headers={**MCP, **headers})
    if r.status_code != 200:
        return r.status_code, r.json()
    msg = next(json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:"))
    return 200, msg["result"]


def _whoami(client: TestClient, headers: dict) -> tuple[int, dict]:
    status, result = _tool(client, headers, "whoami", {})
    return status, json.loads(result["content"][0]["text"]) if status == 200 else result


def _allowlist(*subjects: str) -> None:
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oauth_allowed_subjects": list(subjects)})


# ---- /s: owner and admin pass-through ------------------------------------------


def test_a_members_jwt_reaches_their_own_oauth_server_off_the_allowlist(plane):
    ada = _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        admin = _enforce()
        slug = _oauth_server(client, admin, "hers", ada)
        _proxying(client)
        _allowlist("someone-else")
        r = client.get(f"/s/{slug}/mcp", headers={**LOOPBACK, **_access(plane, "sub-ada")})
        assert r.status_code == 200, r.text


def test_a_members_jwt_is_refused_on_another_members_server(plane):
    ada = _linked("ada@example.com", "sub-ada")
    bob = _linked("bob@example.com", "sub-bob")
    with TestClient(app) as client:
        admin = _enforce()
        hers = _oauth_server(client, admin, "hers", ada)
        his = _oauth_server(client, admin, "his", bob)
        _proxying(client)
        _allowlist("someone-else")
        token = {**LOOPBACK, **_access(plane, "sub-ada")}
        assert client.get(f"/s/{hers}/mcp", headers=token).status_code == 200
        assert client.get(f"/s/{his}/mcp", headers=token).status_code == 403


def test_an_admins_jwt_passes_on_any_oauth_server(plane):
    ada = _linked("ada@example.com", "sub-ada")
    _linked("root@example.com", "sub-root", role="admin")
    with TestClient(app) as client:
        admin = _enforce()
        hers = _oauth_server(client, admin, "hers", ada)
        unowned = _oauth_server(client, admin, "unowned", None)
        _proxying(client)
        _allowlist("someone-else")
        token = {**LOOPBACK, **_access(plane, "sub-root")}
        assert client.get(f"/s/{hers}/mcp", headers=token).status_code == 200
        assert client.get(f"/s/{unowned}/mcp", headers=token).status_code == 200


def test_an_unresolved_jwt_follows_the_allowlist(plane):
    ada = _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        admin = _enforce()
        slug = _oauth_server(client, admin, "hers", ada)
        _proxying(client)
        _allowlist("sub-listed")
        assert client.get(f"/s/{slug}/mcp", headers={
            **LOOPBACK, **_access(plane, "sub-listed")}).status_code == 200
        assert client.get(f"/s/{slug}/mcp", headers={
            **LOOPBACK, **_access(plane, "sub-stranger")}).status_code == 403
        # The owner, unlisted, gets in by their Linked identity.
        assert client.get(f"/s/{slug}/mcp", headers={
            **LOOPBACK, **_access(plane, "sub-ada")}).status_code == 200


# ---- the control MCP ------------------------------------------------------------


def test_a_jwt_drives_whoami_as_its_user(plane):
    _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        _enforce()
        status, me = _whoami(client, _access(plane, "sub-ada"))
        assert status == 200, me
        assert me["user"]["name"] == "ada@example.com" and me["user"]["role"] == "member"


def test_the_per_user_control_mcp_switch_applies_to_a_jwt(plane):
    ada = _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        _enforce()
        assert _whoami(client, _access(plane, "sub-ada"))[0] == 200
        with Session(get_engine()) as s:
            user = s.get(User, ada)
            user.control_mcp = False
            s.add(user)
            s.commit()
        status, body = _whoami(client, _access(plane, "sub-ada"))
        assert status == 403 and "disabled for this user" in body["detail"]


def test_a_jwt_from_a_different_issuer_never_resolves(plane):
    _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        _enforce()
        assert _whoami(client, _access(plane, "sub-ada"))[0] == 200
        status, _ = _whoami(client, _access(plane, "sub-ada", iss="https://other.example"))
        assert status == 403


def test_a_different_oauth_issuer_never_resolves(plane, monkeypatch):
    # The oauth provider trusts another issuer than SSO does: nothing resolves, even
    # a token whose iss is the SSO issuer.
    _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        _enforce()
        assert _whoami(client, _access(plane, "sub-ada"))[0] == 200
        _install(plane, monkeypatch, "https://other.example")
        assert _whoami(client, _access(plane, "sub-ada"))[0] == 403


def test_an_entra_tenant_not_on_the_list_never_resolves(plane, monkeypatch):
    plane.discovery_issuer = ENTRA
    _install(plane, monkeypatch, ENTRA)
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oidc_allowed_tenants": [TENANT]})
    listed = ENTRA.replace("{tenantid}", TENANT)
    unlisted = ENTRA.replace("{tenantid}", OTHER_TENANT)
    _linked("ada@example.com", "sub-ada", issuer=listed)
    _linked("eve@example.com", "sub-eve", issuer=unlisted)
    with TestClient(app) as client:
        _enforce()
        status, me = _whoami(client, _access(plane, "sub-ada", iss=listed, tid=TENANT))
        assert status == 200 and me["user"]["name"] == "ada@example.com"
        status, _ = _whoami(client, _access(plane, "sub-eve", iss=unlisted, tid=OTHER_TENANT))
        assert status == 403
        # A listed tid can't vouch for another tenant's iss either.
        status, _ = _whoami(client, _access(plane, "sub-eve", iss=unlisted, tid=TENANT))
        assert status == 403


def test_a_deactivated_users_jwt_is_refused(plane):
    ada = _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        admin = _enforce()
        slug = _oauth_server(client, admin, "hers", ada)
        _proxying(client)
        _allowlist("someone-else")
        token = _access(plane, "sub-ada")
        assert _whoami(client, token)[0] == 200
        assert client.get(f"/s/{slug}/mcp", headers={**LOOPBACK, **token}).status_code == 200
        r = client.patch(f"/api/users/{ada}", json={"active": False}, headers=admin)
        assert r.status_code == 200, r.text
        assert _whoami(client, token)[0] == 403
        assert client.get(f"/s/{slug}/mcp", headers={**LOOPBACK, **token}).status_code == 403


def test_an_hs256_token_never_resolves(plane):
    _linked("ada@example.com", "sub-ada")
    plane.client_secret = "shared-secret-shared-secret-shared"
    with TestClient(app) as client:
        _enforce()
        assert _whoami(client, _access(plane, "sub-ada"))[0] == 200
        status, _ = _whoami(client, _access(plane, "sub-ada", alg="HS256"))
        assert status == 403


def test_no_user_row_changes_after_jwt_requests(plane):
    ada = _linked("ada@example.com", "sub-ada")
    # Unlinked, but named the way provisioning or matching would find them.
    with Session(get_engine()) as s:
        repo.create_user(s, User(id=new_id(), name="bob@example.com", role="member"))
        runtime_settings.write(s, {
            "oidc_provisioning": True, "oidc_admin_groups": ["admins"],
        })

    def users() -> list[dict]:
        with Session(get_engine()) as s:
            return [u.model_dump() for u in s.exec(select(User).order_by(User.id)).all()]

    with TestClient(app) as client:
        admin = _enforce()
        slug = _oauth_server(client, admin, "hers", ada)
        _proxying(client)
        before = users()
        for sub, extra in (
            ("sub-ada", {"groups": ["admins"], "email": "ada@example.com"}),
            ("sub-bob", {"email": "bob@example.com", "email_verified": True}),
            ("sub-new", {"email": "new@example.com", "email_verified": True}),
        ):
            token = _access(plane, sub, **extra)
            _whoami(client, token)
            client.get(f"/s/{slug}/mcp", headers={**LOOPBACK, **token})
        assert _whoami(client, _access(plane, "sub-ada"))[0] == 200
        assert users() == before


def test_a_jwt_is_not_a_control_credential_on_the_rest_of_api(plane):
    _linked("ada@example.com", "sub-ada", role="admin")
    with TestClient(app) as client:
        _enforce()
        token = _access(plane, "sub-ada")
        assert _whoami(client, token)[0] == 200
        assert client.get("/api/servers", headers={**LOOPBACK, **token}).status_code == 401
        status = client.get("/api/auth/status", headers={**LOOPBACK, **token}).json()
        assert status.get("user") is None


def test_a_jwt_without_a_kid_from_a_single_key_provider_resolves(plane):
    _linked("ada@example.com", "sub-ada")
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "sub-ada", "iat": now, "exp": now + 300},
        plane._keys[0][1], algorithm="RS256",
    )
    assert "kid" not in jwt.get_unverified_header(token)
    with TestClient(app) as client:
        _enforce()
        status, me = _whoami(client, {"authorization": f"Bearer {token}"})
        assert status == 200 and me["user"]["name"] == "ada@example.com", me


def test_the_first_jwt_after_a_restart_resolves_with_a_cold_key_cache(plane, monkeypatch):
    _linked("ada@example.com", "sub-ada")
    monkeypatch.setattr(jwks, "_good", {})  # no keys loaded yet, as right after a boot
    # A real fetch takes a network round trip, so the background refresh an on-loop
    # lookup starts doesn't land before that lookup answers. Model that exactly.
    monkeypatch.setattr(jwks, "_refresh_soon", lambda url, *, refresh: None)
    with TestClient(app) as client:
        _enforce()
        status, me = _whoami(client, _access(plane, "sub-ada"))
        assert status == 200 and me["user"]["name"] == "ada@example.com", me


# ---- only a JWT-shaped bearer ever reaches the provider ---------------------------


@pytest.fixture
def unreachable(plane, monkeypatch):
    """The provider is down and nothing is cached: any discovery or key fetch is seen."""
    from app.auth import oidc
    from app.config import get_settings

    fetched: list[str] = []

    async def discovery(url):
        fetched.append(url)
        raise httpx.ConnectError("down")

    def keys(url, token):
        fetched.append(url)
        raise jwt.PyJWKClientConnectionError("down")

    plane.down = True
    monkeypatch.setattr(oauth_mod, "_discovery_cache", {})
    monkeypatch.setattr(oauth_mod, "_discovery", discovery)
    monkeypatch.setattr(oidc, "fetch_discovery", discovery)
    monkeypatch.setattr(jwks, "signing_key", keys)
    monkeypatch.setattr(get_settings(), "admin_token", "breakglass-secret")
    yield fetched
    assert plane.seen == []


def test_the_break_glass_token_never_waits_on_the_provider(unreachable):
    with TestClient(app) as client:
        _enforce()
        _proxying(client)
        glass = {"authorization": "Bearer breakglass-secret"}
        assert _whoami(client, glass)[0] == 200
        r = client.get(f"/s/{_builtin_slug()}/mcp", headers={**LOOPBACK, **glass})
        assert r.status_code == 200, r.text
    assert unreachable == []


def test_a_bearer_that_isnt_jwt_shaped_never_reaches_the_provider(unreachable):
    with TestClient(app) as client:
        _enforce()
        for junk in ("not-a-jwt", "a.b.c", "e30.e30", "W10.e30.sig"):  # W10 = "[]"
            assert _whoami(client, {"authorization": f"Bearer {junk}"})[0] in (401, 403)
    assert unreachable == []


# ---- malformed headers are refused, never a 500 ----------------------------------


@pytest.mark.parametrize("header", [
    {"alg": ["RS256"]}, {"alg": {"RS256": 1}}, {"alg": None}, {"alg": "RS256", "kid": 7},
])
def test_a_malformed_jwt_header_is_refused_cleanly(plane, header):
    import base64

    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    _linked("ada@example.com", "sub-ada")
    good = _access(plane, "sub-ada")["authorization"].split()[1]  # live: only the header is bad
    body, sig = good.split(".")[1:]
    token = ".".join([part({"typ": "JWT", **header}), body, sig])
    with TestClient(app, raise_server_exceptions=False) as client:
        _enforce()
        status, _ = _whoami(client, {"authorization": f"Bearer {token}"})
        assert status in (401, 403)


# ---- enforcement off: a JWT still names its user for the control_mcp switch ------


def _switch(uid: str, on: bool) -> None:
    with Session(get_engine()) as s:
        user = s.get(User, uid)
        user.control_mcp = on
        s.add(user)
        s.commit()


@pytest.mark.parametrize("on", [True, False])
def test_with_enforcement_off_a_jwt_applies_its_users_control_mcp_switch(plane, on):
    ada = _linked("ada@example.com", "sub-ada")
    _switch(ada, on)
    with TestClient(app) as client:
        status, body = _whoami(client, _access(plane, "sub-ada"))
        if on:
            assert status == 200, body  # as today: the local admin
        else:
            assert status == 403, body
            assert "disabled for this user" in body["detail"]


def test_with_enforcement_off_no_credential_is_still_the_local_admin(plane):
    with TestClient(app) as client:
        assert _whoami(client, {})[0] == 200


# ---- /s and /g: the built-in server opens to a resolved JWT ----------------------


def _builtin_slug() -> str:
    with Session(get_engine()) as s:
        return service.get_builtin(s).slug


def test_a_members_jwt_opens_the_builtin_servers_s_url(plane):
    _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        _enforce()
        _proxying(client)
        token = _access(plane, "sub-ada")
        r = client.get(f"/s/{_builtin_slug()}/mcp", headers={**LOOPBACK, **token})
        assert r.status_code == 200, r.text
        status, me = _whoami(client, token)
        assert status == 200 and me["user"]["name"] == "ada@example.com", me


def test_a_members_jwt_cannot_open_a_group_with_the_builtin_server(plane):
    import asyncio

    from fastapi import HTTPException

    from app.auth.bearer import BearerProvider
    from app.groups import registry
    from app.groups.hub import group_server

    _linked("ada@example.com", "sub-ada")
    _linked("root@example.com", "sub-root", role="admin")
    with TestClient(app) as client:
        _enforce()
        with Session(get_engine()) as s:
            registry.write_group(s, "ops", [service.get_builtin(s).id])
        member, root = _access(plane, "sub-ada"), _access(plane, "sub-root")
        assert _whoami(client, member)[0] == 200  # warms the discovery caches

        def auth(token: dict[str, str]) -> None:
            header = token["authorization"].encode()
            req = Request({"type": "http", "headers": [(b"authorization", header)]})
            asyncio.run(BearerProvider().authenticate(req, group_server("ops")))

        auth(root)  # an admin's credential opens it, as before
        with pytest.raises(HTTPException):
            auth(member)


def test_a_jwt_never_opens_an_ordinary_bearer_server(plane):
    _linked("ada@example.com", "sub-ada", role="admin")
    with TestClient(app) as client:
        admin = _enforce()
        r = client.post(
            "/api/servers", json={"name": "plain", "command": "echo", "auth_provider": "bearer"},
            headers=admin,
        )
        assert r.status_code == 201, r.text
        _proxying(client)
        r = client.get(f"/s/{r.json()['slug']}/mcp", headers={**LOOPBACK, **_access(plane, "sub-ada")})
        assert r.status_code in (401, 403), r.text


# ---- settings: the lock-out guard classifies a JWT like the gate does --------------


def test_an_admins_jwt_changes_settings_through_the_control_mcp(plane):
    _linked("ada@example.com", "sub-ada", role="admin")
    with TestClient(app) as client:
        _enforce()
        status, result = _tool(
            client, _access(plane, "sub-ada"), "update_settings",
            {"changes": {"idle_timeout_s": 120}},
        )
        assert status == 200
        assert not result.get("isError"), result
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["idle_timeout_s"] == 120


def test_a_members_jwt_still_cannot_enable_enforcement(plane):
    _linked("bob@example.com", "sub-bob")
    with TestClient(app) as client:
        _enforce()
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": "auto"})
        status, result = _tool(
            client, _access(plane, "sub-bob"), "update_settings",
            {"changes": {"control_plane_auth": "always"}},
        )
        assert status != 200 or result.get("isError"), result
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["control_plane_auth"] == "auto"


# ---- refresh: a queued JWT principal fails closed --------------------------------


def _jwt_principal(token: dict[str, str]):
    header = token["authorization"].encode()
    request = Request({"type": "http", "headers": [(b"authorization", header)]})
    with Session(get_engine()) as s:
        return principal.credential_principal(request, s, cloudflare=False, provider_jwt=True)


def _refreshed(queued):
    with Session(get_engine()) as s:
        return principal.refresh(s, queued)


@pytest.mark.parametrize("change", ["deactivate", "unlink"])
def test_a_queued_jwt_principal_fails_closed_at_refresh(plane, change):
    ada = _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        admin = _enforce()
        token = _access(plane, "sub-ada")
        assert _whoami(client, token)[0] == 200  # warms the discovery caches
        queued = _jwt_principal(token)
        assert queued is not None and queued.user_id == ada
        assert _refreshed(queued) is not None
        if change == "deactivate":
            r = client.patch(f"/api/users/{ada}", json={"active": False}, headers=admin)
            assert r.status_code == 200, r.text
        else:
            assert _unlink(client, ada, admin).status_code == 204
        assert _refreshed(queued) is None


# ---- oauth_member_access: owner_only (#275) --------------------------------------


def _member_access(mode: str) -> None:
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"oauth_member_access": mode})


def test_owner_only_refuses_a_member_on_another_members_server_even_when_allowlisted(plane):
    ada = _linked("ada@example.com", "sub-ada")
    bob = _linked("bob@example.com", "sub-bob")
    with TestClient(app) as client:
        admin = _enforce()
        hers = _oauth_server(client, admin, "hers", ada)
        his = _oauth_server(client, admin, "his", bob)
        _proxying(client)
        _member_access("owner_only")
        _allowlist("sub-ada")
        token = {**LOOPBACK, **_access(plane, "sub-ada")}
        assert client.get(f"/s/{hers}/mcp", headers=token).status_code == 200
        r = client.get(f"/s/{his}/mcp", headers=token)
        assert r.status_code == 403 and "belongs to another user" in r.json()["detail"]


def test_owner_only_refuses_a_member_when_no_allowlist_is_set(plane):
    _linked("ada@example.com", "sub-ada")
    bob = _linked("bob@example.com", "sub-bob")
    with TestClient(app) as client:
        admin = _enforce()
        his = _oauth_server(client, admin, "his", bob)
        _proxying(client)
        token = {**LOOPBACK, **_access(plane, "sub-ada")}
        assert client.get(f"/s/{his}/mcp", headers=token).status_code == 200  # additive
        _member_access("owner_only")
        assert client.get(f"/s/{his}/mcp", headers=token).status_code == 403


def test_owner_only_lets_an_admin_through_everywhere(plane):
    ada = _linked("ada@example.com", "sub-ada")
    _linked("root@example.com", "sub-root", role="admin")
    with TestClient(app) as client:
        admin = _enforce()
        hers = _oauth_server(client, admin, "hers", ada)
        unowned = _oauth_server(client, admin, "unowned", None)
        _proxying(client)
        _member_access("owner_only")
        _allowlist("someone-else")
        token = {**LOOPBACK, **_access(plane, "sub-root")}
        assert client.get(f"/s/{hers}/mcp", headers=token).status_code == 200
        assert client.get(f"/s/{unowned}/mcp", headers=token).status_code == 200


@pytest.mark.parametrize("mode", ["additive", "owner_only"])
def test_an_unresolved_jwt_follows_the_allowlist_in_both_modes(plane, mode):
    ada = _linked("ada@example.com", "sub-ada")
    with TestClient(app) as client:
        admin = _enforce()
        slug = _oauth_server(client, admin, "hers", ada)
        _proxying(client)
        _member_access(mode)
        _allowlist("sub-listed")
        assert client.get(f"/s/{slug}/mcp", headers={
            **LOOPBACK, **_access(plane, "sub-listed")}).status_code == 200
        assert client.get(f"/s/{slug}/mcp", headers={
            **LOOPBACK, **_access(plane, "sub-stranger")}).status_code == 403


def test_an_invalid_oauth_member_access_is_a_400(plane):
    with TestClient(app) as client:
        admin = _enforce()
        r = client.patch("/api/settings", json={"oauth_member_access": "everyone"}, headers=admin)
        assert r.status_code == 400 and "oauth_member_access" in r.json()["detail"]
        assert client.get("/api/settings", headers=admin).json()["oauth_member_access"] == "additive"
        r = client.patch("/api/settings", json={"oauth_member_access": "owner_only"}, headers=admin)
        assert r.status_code == 200 and r.json()["oauth_member_access"] == "owner_only"


@pytest.mark.parametrize("mode", ["additive", "owner_only"])
def test_an_oauth_group_under_each_member_access_mode(plane, mode):
    from app.groups import registry

    _linked("ada@example.com", "sub-ada")
    _linked("root@example.com", "sub-root", role="admin")
    with TestClient(app) as client:
        _enforce()
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"default_auth_provider": "oauth"})
            registry.write_group(s, "team", [])
        try:
            _member_access(mode)
            _allowlist("sub-ada", "sub-listed")

            def status(sub: str) -> int:
                return client.get("/g/team/mcp", headers={**MCP, **_access(plane, sub)}).status_code

            # Allowlisted, a resolved member passes under additive. Under owner_only a
            # group is admin-owned, so they're refused even though the list names them.
            assert (status("sub-ada") == 403) is (mode == "owner_only")
            assert status("sub-root") != 403
            assert status("sub-listed") != 403
            assert status("sub-stranger") == 403
        finally:
            with Session(get_engine()) as s:
                registry.delete_group(s, "team")
                runtime_settings.write(s, {"default_auth_provider": "none"})
