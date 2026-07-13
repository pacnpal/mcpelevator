"""Upstream-OAuth tests — the token store, the flow plumbing, and the API surface.

Covers the three moving parts that make OAuth for a remote server work end to end:

* ``ServerTokenStorage`` — the file store the control plane and the bridge share
  (round-trips, status snapshot, 0600 perms, clear).
* ``oauth_flow`` — the interactive begin/complete handshake, driven with a fake
  httpx client that stands in for the upstream + provider so no network is touched.
* the API — create/detail exposure, the authorize/callback/disconnect endpoints,
  and the guardrails (non-OAuth server, unknown callback state).
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthToken

from app.auth import oauth_flow
from app.auth.oauth_store import ServerTokenStorage
from app.main import app
from app.registry import service

LOOPBACK = {"host": "127.0.0.1"}


# --------------------------------------------------------------------------- #
# token store
# --------------------------------------------------------------------------- #


async def test_token_store_roundtrip_and_status():
    store = ServerTokenStorage("srv-store-1")
    store.clear()
    try:
        # No file yet -> unauthenticated, nothing stored.
        assert store.status() == {
            "authenticated": False,
            "expires_at": None,
            "has_refresh_token": False,
        }
        assert await store.get_tokens() is None

        await store.set_tokens(
            OAuthToken(access_token="AT", token_type="Bearer", refresh_token="RT", expires_in=3600)
        )
        got = await store.get_tokens()
        assert got is not None and got.access_token == "AT"

        st = store.status()
        assert st["authenticated"] is True
        assert st["has_refresh_token"] is True
        assert st["expires_at"] is not None  # absolute expiry persisted
    finally:
        store.clear()


async def test_token_store_file_is_private():
    store = ServerTokenStorage("srv-store-perms")
    store.clear()
    try:
        await store.set_tokens(OAuthToken(access_token="AT", token_type="Bearer"))
        mode = os.stat(store.path).st_mode & 0o777
        assert mode == 0o600, oct(mode)
    finally:
        store.clear()


def test_token_store_clear_is_idempotent():
    store = ServerTokenStorage("srv-store-missing")
    store.clear()  # file doesn't exist -> no error
    assert store.status()["authenticated"] is False


def test_token_store_rejects_traversal_id():
    # A server id that could escape the oauth directory must be refused before any FS op.
    with pytest.raises(ValueError):
        ServerTokenStorage("../evil")
    with pytest.raises(ValueError):
        ServerTokenStorage("a/b")


async def test_clear_tokens_keeps_client_info():
    from mcp.shared.auth import OAuthClientInformationFull

    store = ServerTokenStorage("srv-cleartokens")
    store.clear()
    try:
        await store.set_client_info(
            OAuthClientInformationFull(client_id="cid", redirect_uris=["http://127.0.0.1/cb"])
        )
        await store.set_tokens(
            OAuthToken(access_token="AT", token_type="Bearer", refresh_token="RT")
        )
        store.clear_tokens()
        assert await store.get_tokens() is None  # tokens gone
        ci = await store.get_client_info()
        assert ci is not None and ci.client_id == "cid"  # client registration preserved
    finally:
        store.clear()


# --------------------------------------------------------------------------- #
# normalize_oauth (service-level rules)
# --------------------------------------------------------------------------- #


def test_normalize_oauth_forced_off_for_non_remote():
    # A local runner can't do upstream OAuth — it's forced off and the fields cleared,
    # so a stray secret can't ride along and the config hash stays stable.
    assert service.normalize_oauth("npx", True, "scope", "cid", "sec") == (False, "", None, None)


def test_normalize_oauth_strips_and_blanks():
    assert service.normalize_oauth("remote", True, "  a b  ", "  ", "  ") == (True, "a b", None, None)


def test_normalize_oauth_secret_without_id_rejected():
    with pytest.raises(ValueError):
        service.normalize_oauth("remote", True, "", None, "secret-without-id")


def test_config_hash_excludes_client_secret():
    # The config fingerprint must never read the client secret (it's a credential and the
    # bridge doesn't consume it from the spec) — neither its value nor its presence affects
    # the hash. The static client is tracked via the non-sensitive client_id, so changing
    # that DOES change the hash.
    from app.db.models import Server

    def mk(client_id, secret):
        return Server(
            id="x", slug="x", name="x", runner="remote",
            command="https://up.example/mcp", args=["streamable-http"], env={},
            oauth=True, oauth_client_id=client_id, oauth_client_secret=secret,
        )

    base = service.compute_hash(mk("cid", None))
    assert service.compute_hash(mk("cid", "secret-A")) == base  # secret value: ignored
    assert service.compute_hash(mk("cid", "secret-B")) == base  # rotating it: ignored
    assert service.compute_hash(mk(None, None)) != base  # client_id change: restarts


# --------------------------------------------------------------------------- #
# flow plumbing (begin -> callback -> complete), no network
# --------------------------------------------------------------------------- #


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: instead of talking to a real upstream, it
    drives the provider's own redirect/callback handlers and stores tokens — exactly
    what a successful OAuth handshake would do."""

    STATE = "test-state-123"

    def __init__(self, *_args, auth=None, **_kwargs):
        self._provider = auth

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _handshake(self):
        ctx = self._provider.context
        # 1) provider hands us the authorization URL -> control plane returns it to the browser
        await ctx.redirect_handler(f"https://auth.example/authorize?state={self.STATE}&code_challenge=x")
        # 2) provider parks until the browser callback delivers the code
        code, state = await ctx.callback_handler()
        assert state == self.STATE
        # 3) the exchange stores tokens (what the real provider does on a 200)
        await ctx.storage.set_tokens(
            OAuthToken(access_token=f"AT-{code}", token_type="Bearer", refresh_token="RT", expires_in=3600)
        )
        return httpx.Response(200)

    def stream(self, _method, _url, **_kwargs):
        # _drive uses client.stream(...) as an async context manager; drive the handshake
        # on entry so the flow completes exactly as with a real streaming response.
        handshake = self._handshake

        class _Ctx:
            async def __aenter__(_self):
                return await handshake()

            async def __aexit__(_self, *exc):
                return False

        return _Ctx()


async def test_flow_begin_and_complete(monkeypatch):
    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _FakeAsyncClient)

    class _Srv:
        id = "srv-flow-1"
        command = "https://up.example/mcp"
        args = ["streamable-http"]
        env: dict = {}
        oauth_client_id = None
        oauth_client_secret = None
        oauth_scopes = ""

    store = ServerTokenStorage(_Srv.id)
    store.clear()
    try:
        url = await oauth_flow.begin_authorization(_Srv, callback_url="http://127.0.0.1:8080/api/oauth/callback")
        assert url.startswith("https://auth.example/authorize")
        assert f"state={_FakeAsyncClient.STATE}" in url

        server_id = await oauth_flow.complete_authorization(_FakeAsyncClient.STATE, code="the-code")
        assert server_id == _Srv.id

        tokens = await store.get_tokens()
        assert tokens is not None and tokens.access_token == "AT-the-code"
    finally:
        store.clear()


async def test_flow_complete_unknown_state_raises():
    with pytest.raises(KeyError):
        await oauth_flow.complete_authorization("nope-not-a-real-state", code="x")


async def test_reauth_preserves_tokens_until_success(monkeypatch):
    # Re-authenticating must not destroy the existing working token up front: the shared
    # store keeps the old credential until the new grant actually succeeds.
    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _FakeAsyncClient)

    class _Srv:
        id = "srv-reauth-1"
        command = "https://up.example/mcp"
        args = ["streamable-http"]
        env: dict = {}
        oauth_client_id = None
        oauth_client_secret = None
        oauth_scopes = ""

    store = ServerTokenStorage(_Srv.id)
    store.clear()
    try:
        await store.set_tokens(
            OAuthToken(access_token="OLD", token_type="Bearer", refresh_token="OLDR")
        )
        await oauth_flow.begin_authorization(_Srv, callback_url="http://127.0.0.1/api/oauth/callback")
        # Flow started, browser not yet returned — the working token is untouched.
        got = await store.get_tokens()
        assert got is not None and got.access_token == "OLD"

        await oauth_flow.complete_authorization(_FakeAsyncClient.STATE, code="new-code")
        got = await store.get_tokens()
        assert got is not None and got.access_token == "AT-new-code"  # replaced only on success
    finally:
        store.clear()


async def test_set_tokens_preserves_refresh_token_when_absent():
    # A refresh response that omits refresh_token (provider not rotating) must not wipe the
    # stored one, or the next refresh has no credential.
    store = ServerTokenStorage("srv-refresh-preserve")
    store.clear()
    try:
        await store.set_tokens(
            OAuthToken(access_token="A1", token_type="Bearer", refresh_token="R1", expires_in=60)
        )
        await store.set_tokens(OAuthToken(access_token="A2", token_type="Bearer", expires_in=60))
        got = await store.get_tokens()
        assert got is not None and got.access_token == "A2" and got.refresh_token == "R1"
    finally:
        store.clear()


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


def _create_oauth_server(c: TestClient) -> str:
    resp = c.post(
        "/api/servers",
        json={
            "name": "oauth-remote",
            "runner": "remote",
            "command": "https://up.example/mcp",
            "args": ["streamable-http"],
            "oauth": True,
            "oauth_scopes": "read write",
        },
        headers=LOOPBACK,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_detail_exposes_oauth_status_needs_auth():
    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        try:
            detail = c.get(f"/api/servers/{sid}", headers=LOOPBACK).json()
            assert detail["oauth"] is True
            assert detail["oauth_scopes"] == "read write"
            # The client secret is write-only — never echoed back.
            assert "oauth_client_secret" not in detail
            assert detail["oauth_has_client_secret"] is False
            status = detail["oauth_status"]
            assert status["enabled"] is True
            assert status["authenticated"] is False
            assert status["needs_auth"] is True
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_update_clears_tokens_on_url_change():
    import anyio

    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        store = ServerTokenStorage(sid)
        anyio.run(store.set_tokens, OAuthToken(access_token="AT", token_type="Bearer"))
        assert store.status()["authenticated"] is True
        try:
            # Changing the upstream URL invalidates tokens bound to the old resource.
            r = c.patch(
                f"/api/servers/{sid}",
                json={"command": "https://other.example/mcp"},
                headers=LOOPBACK,
            )
            assert r.status_code == 200, r.text
            assert store.status()["authenticated"] is False
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_update_preserves_explicit_null_client_id():
    with TestClient(app) as c:
        r = c.post(
            "/api/servers",
            json={
                "name": "oauth-static",
                "runner": "remote",
                "command": "https://up.example/mcp",
                "args": ["streamable-http"],
                "oauth": True,
                "oauth_client_id": "cid",
            },
            headers=LOOPBACK,
        )
        sid = r.json()["id"]
        try:
            # An explicit null must switch static-client -> DCR, not be dropped as "unchanged".
            r = c.patch(f"/api/servers/{sid}", json={"oauth_client_id": None}, headers=LOOPBACK)
            assert r.status_code == 200, r.text
            detail = c.get(f"/api/servers/{sid}", headers=LOOPBACK).json()
            assert detail["oauth_client_id"] is None
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_authorize_non_oauth_server_is_400():
    with TestClient(app) as c:
        resp = c.post(
            "/api/servers",
            json={"name": "plain", "runner": "remote", "command": "https://up.example/mcp"},
            headers=LOOPBACK,
        )
        sid = resp.json()["id"]
        try:
            r = c.post(f"/api/servers/{sid}/oauth/authorize", headers=LOOPBACK)
            assert r.status_code == 400
            assert "oauth" in r.json()["detail"].lower()
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_authorize_returns_url(monkeypatch):
    async def _fake_begin(server, *, callback_url):
        assert callback_url.endswith("/api/oauth/callback")
        return "https://auth.example/authorize?state=abc"

    monkeypatch.setattr(oauth_flow, "begin_authorization", _fake_begin)
    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        try:
            r = c.post(f"/api/servers/{sid}/oauth/authorize", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            assert r.json()["authorize_url"] == "https://auth.example/authorize?state=abc"
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_callback_unknown_state_redirects_with_error():
    with TestClient(app) as c:
        r = c.get(
            "/api/oauth/callback",
            params={"code": "x", "state": "bogus"},
            headers=LOOPBACK,
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "oauth=error" in r.headers["location"]


def test_callback_success_redirects_to_server(monkeypatch):
    with TestClient(app) as c:
        sid = _create_oauth_server(c)

        async def _fake_complete(state, code):
            assert (state, code) == ("st", "cd")
            return sid

        monkeypatch.setattr(oauth_flow, "complete_authorization", _fake_complete)
        try:
            r = c.get(
                "/api/oauth/callback",
                params={"code": "cd", "state": "st"},
                headers=LOOPBACK,
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert r.headers["location"] == f"/server/{sid}?oauth=connected"
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_disconnect_clears_tokens():
    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        try:
            # Seed a token file, then disconnect and confirm it's cleared.
            import anyio

            store = ServerTokenStorage(sid)
            anyio.run(store.set_tokens, OAuthToken(access_token="AT", token_type="Bearer"))
            assert store.status()["authenticated"] is True

            r = c.post(f"/api/servers/{sid}/oauth/disconnect", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            assert r.json()["oauth_status"]["authenticated"] is False
            assert store.status()["authenticated"] is False
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


# --------------------------------------------------------------------------- #
# bridge wiring
# --------------------------------------------------------------------------- #


def test_bridge_builds_oauth_auth_and_preloads_metadata():
    from mcp.client.auth import OAuthClientProvider

    from app.bridge import host

    oauth = {"server_id": "srv-bridge-1", "url": "https://up.example/mcp", "scopes": "read"}
    auth = host._build_oauth_auth(oauth)
    assert isinstance(auth, OAuthClientProvider)
    # No stored metadata yet -> context.oauth_metadata stays unset.
    assert getattr(auth.context, "oauth_metadata", None) is None


def test_bridge_transport_passes_oauth_auth(monkeypatch):
    from app.bridge import host

    captured = {}

    class _FakeSHTTP:
        def __init__(self, url, headers=None, auth=None):
            captured["url"] = url
            captured["auth"] = auth

    monkeypatch.setattr(host, "StreamableHttpTransport", _FakeSHTTP)
    spec = {
        "command": "https://up.example/mcp",
        "transport": "streamable-http",
        "env": {},
        "oauth": {"server_id": "srv-bridge-2", "url": "https://up.example/mcp", "scopes": ""},
    }
    host._build_transport(spec)
    from mcp.client.auth import OAuthClientProvider

    assert isinstance(captured["auth"], OAuthClientProvider)


def test_bridge_transport_without_oauth_has_no_auth(monkeypatch):
    from app.bridge import host

    captured = {}

    class _FakeSHTTP:
        def __init__(self, url, headers=None, auth=None):
            captured["auth"] = auth

    monkeypatch.setattr(host, "StreamableHttpTransport", _FakeSHTTP)
    host._build_transport(
        {"command": "https://up.example/mcp", "transport": "streamable-http", "env": {"A": "b"}}
    )
    assert captured["auth"] is None


def test_delete_server_clears_oauth_tokens():
    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        import anyio

        store = ServerTokenStorage(sid)
        anyio.run(store.set_tokens, OAuthToken(access_token="AT", token_type="Bearer"))
        assert store.path.exists()

        c.delete(f"/api/servers/{sid}", headers=LOOPBACK)
        assert not store.path.exists()
