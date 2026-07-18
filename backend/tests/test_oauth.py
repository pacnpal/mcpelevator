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


def test_normalize_oauth_strips_scopes_and_id_but_preserves_secret():
    # Scopes and client_id are trimmed; the client secret is an OPAQUE credential and is
    # kept VERBATIM (edge whitespace can be part of a real provider-issued secret).
    assert service.normalize_oauth("remote", True, "  a b  ", "  cid  ", "  s3cret ") == (
        True,
        "a b",
        "cid",
        "  s3cret ",
    )
    # A whitespace-only client_id collapses to None, and an empty secret is absent.
    assert service.normalize_oauth("remote", True, "  a b  ", "  ", "") == (True, "a b", None, None)


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


async def test_flow_rejects_grant_the_endpoint_still_refuses(monkeypatch):
    # The code exchange succeeds (tokens land in the ephemeral store) but the retried MCP probe
    # still comes back 401 — the resource rejected the token (wrong resource / missing scope).
    # The unusable grant must NOT be promoted, and the flow must surface as a failure.
    class _Reject401(_FakeAsyncClient):
        async def _handshake(self):
            ctx = self._provider.context
            await ctx.redirect_handler(f"https://auth.example/authorize?state={self.STATE}")
            code, _state = await ctx.callback_handler()
            await ctx.storage.set_tokens(
                OAuthToken(access_token=f"AT-{code}", token_type="Bearer", refresh_token="RT", expires_in=3600)
            )
            return httpx.Response(401)

    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _Reject401)

    class _Srv:
        id = "srv-flow-reject"
        command = "https://up.example/mcp"
        args = ["streamable-http"]
        env: dict = {}
        oauth_client_id = None
        oauth_client_secret = None
        oauth_scopes = ""

    store = ServerTokenStorage(_Srv.id)
    store.clear()
    try:
        await oauth_flow.begin_authorization(_Srv, callback_url="http://127.0.0.1/api/oauth/callback")
        with pytest.raises(Exception):
            await oauth_flow.complete_authorization(_FakeAsyncClient.STATE, code="c")
        # Nothing promoted — the server still has no usable credential.
        assert store.status()["authenticated"] is False
    finally:
        store.clear()


async def test_registration_rate_limit_surfaces_clean_429(monkeypatch):
    # A 429 from Dynamic Client Registration (before any authorization URL is produced) must
    # surface as a clean OAuthBeginError carrying status 429 — not the provider's raw JSON as a
    # 502 — and must leave no pending flow behind.
    from mcp.client.auth import OAuthRegistrationError

    class _RateLimited(_FakeAsyncClient):
        def stream(self, _method, _url, **_kwargs):
            class _Ctx:
                async def __aenter__(_self):
                    raise OAuthRegistrationError(
                        'Registration failed: 429 {"error":"too_many_requests"}'
                    )

                async def __aexit__(_self, *exc):
                    return False

            return _Ctx()

    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _RateLimited)

    class _Srv:
        id = "srv-ratelimit-1"
        command = "https://up.example/mcp"
        args = ["streamable-http"]
        env: dict = {}
        oauth_client_id = None
        oauth_client_secret = None
        oauth_scopes = ""

    store = ServerTokenStorage(_Srv.id)
    store.clear()
    try:
        with pytest.raises(oauth_flow.OAuthBeginError) as excinfo:
            await oauth_flow.begin_authorization(
                _Srv, callback_url="http://127.0.0.1/api/oauth/callback"
            )
        assert excinfo.value.status_code == 429
        assert "rate-limiting" in str(excinfo.value)
        # The failed flow was forgotten — no dangling pending/state for this server.
        assert all(p.server_id != _Srv.id for p in oauth_flow._PENDING.values())
    finally:
        store.clear()


async def test_drive_cancels_done_future_on_pre_url_failure(monkeypatch):
    # Regression: when the flow fails BEFORE handing back an authorization URL, done_future is
    # never awaited by anyone (complete_authorization only runs after the browser returns). It
    # must be cancelled, not loaded with an exception — an unretrieved future-exception logs a
    # spurious "Future exception was never retrieved" when the pending object is collected.
    class _Boom(_FakeAsyncClient):
        def stream(self, _method, _url, **_kwargs):
            class _Ctx:
                async def __aenter__(_self):
                    raise RuntimeError("failed before url")

                async def __aexit__(_self, *exc):
                    return False

            return _Ctx()

    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _Boom)

    class _Srv:
        id = "srv-drive-preurl"
        command = "https://up.example/mcp"
        args = ["streamable-http"]
        env: dict = {}

    pending = oauth_flow._Pending(_Srv.id)
    mem = oauth_flow._MemoryTokenStorage()
    real = ServerTokenStorage(_Srv.id)
    real.clear()
    try:
        # provider is unused on this path (the fake client ignores auth); pass a placeholder.
        await oauth_flow._drive(_Srv, object(), mem, real, pending)
        # url_future carries the error for begin_authorization to retrieve and classify...
        assert isinstance(pending.url_future.exception(), RuntimeError)
        # ...while done_future is cancelled (silent), never exception-loaded.
        assert pending.done_future.cancelled()
    finally:
        real.clear()


async def test_drive_cancels_done_future_when_callback_never_arrives(monkeypatch):
    # Regression: the authorization URL was produced, but the operator never finished in the
    # browser, so the provider's callback wait times out and _drive fails POST-URL with
    # callback_event still unset. complete_authorization never ran, so done_future is awaited by
    # no one — it must be cancelled, not exception-loaded (which would warn on collection).
    class _Boom(_FakeAsyncClient):
        def stream(self, _method, _url, **_kwargs):
            class _Ctx:
                async def __aenter__(_self):
                    raise RuntimeError("timed out waiting for the operator to finish signing in")

                async def __aexit__(_self, *exc):
                    return False

            return _Ctx()

    monkeypatch.setattr(oauth_flow.httpx, "AsyncClient", _Boom)

    class _Srv:
        id = "srv-drive-nocb"
        command = "https://up.example/mcp"
        args = ["streamable-http"]
        env: dict = {}

    pending = oauth_flow._Pending(_Srv.id)
    pending.url_future.set_result("https://auth.example/authorize?state=s")  # URL already handed back
    # callback_event intentionally left unset — the browser never returned.
    mem = oauth_flow._MemoryTokenStorage()
    real = ServerTokenStorage(_Srv.id)
    real.clear()
    try:
        await oauth_flow._drive(_Srv, object(), mem, real, pending)
        assert pending.done_future.cancelled()  # silent — no "Future exception was never retrieved"
    finally:
        real.clear()


def test_registration_status_parses_status_code():
    from mcp.client.auth import OAuthRegistrationError

    assert (
        oauth_flow._registration_status(
            OAuthRegistrationError('Registration failed: 429 {"error":"too_many_requests"}')
        )
        == 429
    )
    # A non-status registration error (e.g. an invalid body) has no code to read.
    assert oauth_flow._registration_status(OAuthRegistrationError("Invalid registration response: x")) is None


def test_classify_begin_error_maps_429_and_falls_back():
    from mcp.client.auth import OAuthRegistrationError

    rate_limited = oauth_flow._classify_begin_error(
        OAuthRegistrationError('Registration failed: 429 {"error":"too_many_requests"}')
    )
    assert rate_limited.status_code == 429 and "rate-limiting" in str(rate_limited)
    # Any other registration failure keeps the generic message + 502.
    other = oauth_flow._classify_begin_error(OAuthRegistrationError("Registration failed: 400 bad"))
    assert other.status_code == 502 and "could not start OAuth" in str(other)
    # An already-classified error passes through unchanged (no double-wrapping).
    pre = oauth_flow.OAuthBeginError("already friendly", status_code=429)
    assert oauth_flow._classify_begin_error(pre) is pre


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


async def test_set_tokens_stamps_default_expiry_when_refreshable():
    # A token response can omit expires_in (RFC 6749 §5.1 makes it optional). If a refresh
    # token IS present, stamp a default expiry so the bridge proactively refreshes instead
    # of treating the token as eternal and letting it 401 into the interactive path.
    store = ServerTokenStorage("srv-default-ttl")
    store.clear()
    try:
        await store.set_tokens(
            OAuthToken(access_token="A", token_type="Bearer", refresh_token="R")
        )
        assert store.get_token_expiry() is not None
        # No refresh token AND no expires_in: a genuinely long-lived opaque token — leave the
        # expiry unset so it keeps being sent rather than being force-refreshed.
        store.clear()
        await store.set_tokens(OAuthToken(access_token="A", token_type="Bearer"))
        assert store.get_token_expiry() is None
    finally:
        store.clear()


def test_promote_carries_forward_metadata_when_absent():
    from mcp.shared.auth import OAuthMetadata

    store = ServerTokenStorage("srv-promote-carry")
    store.clear()
    try:
        meta = OAuthMetadata(
            issuer="https://as.example",
            authorization_endpoint="https://as.example/authorize",
            token_endpoint="https://as.example/token",
            response_types_supported=["code"],
        )
        store.set_metadata(meta)
        # Promote a fresh grant WITHOUT re-supplying metadata (e.g. a grant path that didn't
        # re-run discovery). The discovered token endpoint must survive so the bridge can
        # still refresh, while the tokens are fully replaced.
        store.promote(tokens=OAuthToken(access_token="NEW", token_type="Bearer", refresh_token="R"))
        kept = store.get_metadata()
        assert kept is not None and str(kept.token_endpoint) == "https://as.example/token"
        assert store.status()["authenticated"] is True
        assert store.get_token_expiry() is not None  # refreshable -> default TTL stamped
    finally:
        store.clear()


async def test_memory_store_persists_dcr_registration_without_tokens():
    # A client the SDK registers mid-flow (no seed, no stored tokens) is written straight to the
    # real store so an abandoned/failed browser step doesn't discard the registration and force a
    # re-register next time. It must persist ONLY the client_info — never fabricate tokens.
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyHttpUrl

    real = ServerTokenStorage("srv-dcr-persist")
    real.clear()
    try:
        mem = oauth_flow._MemoryTokenStorage(persist_registration_to=real)
        ci = OAuthClientInformationFull(
            client_id="dcr-123",
            redirect_uris=[AnyHttpUrl("https://mcpe.example/api/oauth/callback")],
        )
        await mem.set_client_info(ci)
        got = await real.get_client_info()
        assert got is not None and got.client_id == "dcr-123"
        assert real.status()["authenticated"] is False  # no tokens fabricated
    finally:
        real.clear()


def test_merge_scopes_unions_and_dedupes():
    assert oauth_flow._merge_scopes("read write", None, "write admin", "") == "read write admin"
    assert oauth_flow._merge_scopes("", None) is None
    assert oauth_flow._merge_scopes(None) is None


def test_offline_access_default_gating():
    from types import SimpleNamespace

    # No metadata discovered yet (oauth_metadata is None) -> best-effort ask.
    assert oauth_flow._offline_access_default(SimpleNamespace(oauth_metadata=None)) is True
    # Metadata present but no scopes_supported list -> ask.
    assert (
        oauth_flow._offline_access_default(
            SimpleNamespace(oauth_metadata=SimpleNamespace(scopes_supported=None))
        )
        is True
    )
    # AS advertises offline_access -> ask.
    assert (
        oauth_flow._offline_access_default(
            SimpleNamespace(
                oauth_metadata=SimpleNamespace(scopes_supported=["read", "offline_access"])
            )
        )
        is True
    )
    # AS publishes a scope list that OMITS it -> don't (a strict AS would reject the
    # whole authorization with invalid_scope on an unadvertised scope).
    assert (
        oauth_flow._offline_access_default(
            SimpleNamespace(oauth_metadata=SimpleNamespace(scopes_supported=["read", "write"]))
        )
        is False
    )


def _make_scoped_provider(operator_scopes):
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyHttpUrl

    async def _redirect(_url):
        return None

    async def _callback():
        return ("code", None)

    return oauth_flow._ScopedOAuthClientProvider(
        server_url="https://up.example/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="mcpelevator",
            redirect_uris=[AnyHttpUrl("http://localhost/cb")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=oauth_flow._MemoryTokenStorage(),
        redirect_handler=_redirect,
        callback_handler=_callback,
        timeout=600.0,
        operator_scopes=operator_scopes,
    )


async def test_grant_requests_offline_access_by_default(monkeypatch):
    from types import SimpleNamespace

    provider = _make_scoped_provider(operator_scopes="read")
    provider.context.client_metadata.scope = "read"  # what the SDK's strategy selected
    provider.context.oauth_metadata = SimpleNamespace(scopes_supported=None)
    captured: dict[str, str] = {}

    async def fake_grant(self):
        captured["scope"] = self.context.client_metadata.scope
        return ("code", "verifier")

    monkeypatch.setattr(
        oauth_flow.OAuthClientProvider, "_perform_authorization_code_grant", fake_grant
    )
    await provider._perform_authorization_code_grant()
    # offline_access (refresh token) AND the operator's scope are both requested.
    assert set(captured["scope"].split()) == {"read", "offline_access"}


async def test_grant_omits_offline_access_when_as_lists_without_it(monkeypatch):
    from types import SimpleNamespace

    provider = _make_scoped_provider(operator_scopes=None)
    provider.context.client_metadata.scope = "read write"
    provider.context.oauth_metadata = SimpleNamespace(scopes_supported=["read", "write"])
    captured: dict[str, str] = {}

    async def fake_grant(self):
        captured["scope"] = self.context.client_metadata.scope
        return ("code", "verifier")

    monkeypatch.setattr(
        oauth_flow.OAuthClientProvider, "_perform_authorization_code_grant", fake_grant
    )
    await provider._perform_authorization_code_grant()
    assert "offline_access" not in (captured["scope"] or "").split()


def test_repair_authorization_url_rejoins_double_question_mark():
    # The SDK appends "?<params>" to the discovered authorization_endpoint; when that
    # endpoint already has a query (e.g. Railway's ?resource=...), the second "?" hides
    # response_type & friends inside the first parameter's value.
    broken = (
        "https://backboard.railway.com/oauth/auth?resource=https%3A%2F%2Fbackboard.railway.com"
        "?response_type=code&client_id=abc&state=xyz"
    )
    fixed = oauth_flow._repair_authorization_url(broken)
    assert fixed == (
        "https://backboard.railway.com/oauth/auth?resource=https%3A%2F%2Fbackboard.railway.com"
        "&response_type=code&client_id=abc&state=xyz"
    )
    # state now parses out of the repaired URL
    assert oauth_flow._extract_state(fixed) == "xyz"


def test_repair_authorization_url_preserves_endpoint_own_question_marks():
    # RFC 3986 permits raw '?' inside a query, so the endpoint's own query may contain
    # them. Only the LAST '?' can be the SDK's separator (urlencode never emits a raw
    # '?'), so everything before it must survive byte-for-byte.
    broken = (
        "https://as.example/auth?resource=https://api.example/mcp?tenant=a"
        "?response_type=code&state=s"
    )
    assert oauth_flow._repair_authorization_url(broken) == (
        "https://as.example/auth?resource=https://api.example/mcp?tenant=a"
        "&response_type=code&state=s"
    )


def test_repair_authorization_url_leaves_wellformed_urls_alone():
    ok = "https://auth.example.com/authorize?response_type=code&state=s1"
    assert oauth_flow._repair_authorization_url(ok) == ok
    assert oauth_flow._repair_authorization_url("https://auth.example.com/authorize") == (
        "https://auth.example.com/authorize"
    )
    # percent-encoded "?" inside a value is a literal, not a separator — untouched
    encoded = "https://auth.example.com/authorize?resource=https%3A%2F%2Fx%3Fy&state=s2"
    assert oauth_flow._repair_authorization_url(encoded) == encoded


def test_ensure_consent_prompt_added_when_offline_access_requested():
    # offline_access in scope + no prompt -> append prompt=consent so an OIDC AS re-consents
    # and mints a refresh token; the existing query is left byte-for-byte intact.
    url = "https://auth.example.com/authorize?scope=read+offline_access&state=s"
    out = oauth_flow._ensure_consent_prompt(url)
    assert out == "https://auth.example.com/authorize?scope=read+offline_access&state=s&prompt=consent"
    assert oauth_flow._extract_state(out) == "s"  # state still parses


def test_ensure_consent_prompt_skips_without_offline_access():
    url = "https://auth.example.com/authorize?scope=read+write&state=s"
    assert oauth_flow._ensure_consent_prompt(url) == url
    # no scope param at all -> unchanged
    bare = "https://auth.example.com/authorize?response_type=code&state=s"
    assert oauth_flow._ensure_consent_prompt(bare) == bare


def test_ensure_consent_prompt_preserves_existing_prompt():
    # A provider-specific prompt value must not be clobbered.
    url = "https://auth.example.com/authorize?scope=offline_access&prompt=login&state=s"
    assert oauth_flow._ensure_consent_prompt(url) == url


def test_ensure_consent_prompt_preserves_railway_style_query():
    # The repaired Railway URL carries a percent-encoded resource param before the SDK's own
    # params; appending prompt=consent must not disturb that value.
    url = (
        "https://backboard.railway.com/oauth/auth?resource=https%3A%2F%2Fbackboard.railway.com"
        "&response_type=code&scope=offline_access&state=xyz"
    )
    out = oauth_flow._ensure_consent_prompt(url)
    assert out == url + "&prompt=consent"
    assert "resource=https%3A%2F%2Fbackboard.railway.com" in out


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


def test_auth_change_stops_stale_oauth_bridge_before_group_resync(monkeypatch):
    events: list[str] = []

    with TestClient(app) as c:
        sid = _create_oauth_server(c)

        async def stop(server_id: str) -> None:
            assert server_id == sid
            events.append("stop")

        async def sync(supervisor) -> None:
            events.append("sync")

        monkeypatch.setattr(c.app.state.supervisor, "stop", stop)
        monkeypatch.setattr(c.app.state.supervisor, "nudge", lambda: None)
        monkeypatch.setattr(c.app.state.groups, "sync", sync)
        try:
            response = c.patch(
                f"/api/servers/{sid}",
                json={
                    "auth_provider": "bearer",
                    "command": "https://other.example/mcp",
                },
                headers=LOOPBACK,
            )
            assert response.status_code == 200, response.text
            assert events[:2] == ["stop", "sync"]
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


def test_authorize_rate_limited_returns_429(monkeypatch):
    async def _fake_begin(server, *, callback_url):
        raise oauth_flow.OAuthBeginError(
            "the OAuth provider is rate-limiting Dynamic Client Registration (HTTP 429). "
            "Wait a minute and try connecting again.",
            status_code=429,
        )

    monkeypatch.setattr(oauth_flow, "begin_authorization", _fake_begin)
    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        try:
            r = c.post(f"/api/servers/{sid}/oauth/authorize", headers=LOOPBACK)
            assert r.status_code == 429, r.text
            assert "rate-limiting" in r.json()["detail"]
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
    import anyio
    from mcp.client.auth import OAuthClientProvider

    from app.bridge import host

    store = ServerTokenStorage("srv-bridge-1")
    store.clear()
    try:
        # A provider is built only once tokens exist (see below); seed one first.
        anyio.run(store.set_tokens, OAuthToken(access_token="AT", token_type="Bearer"))
        oauth = {"server_id": "srv-bridge-1", "url": "https://up.example/mcp", "scopes": "read"}
        auth = host._build_oauth_auth(oauth)
        assert isinstance(auth, OAuthClientProvider)
        # No stored metadata yet -> context.oauth_metadata stays unset.
        assert getattr(auth.context, "oauth_metadata", None) is None
    finally:
        store.clear()


def test_bridge_no_oauth_auth_until_tokens_exist():
    # An unauthenticated OAuth server (empty token file) must NOT get an OAuth provider:
    # otherwise the SDK's 401 path would Dynamic-Client-Register against the upstream on every
    # readiness probe, burning registration quota. The bridge attaches no auth so the probe
    # just 401s cleanly and the server surfaces as needing sign-in.
    from app.bridge import host

    store = ServerTokenStorage("srv-bridge-noauth")
    store.clear()
    try:
        oauth = {"server_id": "srv-bridge-noauth", "url": "https://up.example/mcp", "scopes": ""}
        assert host._build_oauth_auth(oauth) is None
    finally:
        store.clear()


def test_bridge_default_expiry_applied_on_refresh_without_expires_in():
    import time

    import anyio

    from app.auth.oauth_store import _DEFAULT_REFRESHABLE_TTL
    from app.bridge import host

    store = ServerTokenStorage("srv-bridge-expiry")
    store.clear()
    try:
        anyio.run(
            store.set_tokens,
            OAuthToken(access_token="AT", token_type="Bearer", expires_in=3600),
        )
        oauth = {"server_id": "srv-bridge-expiry", "url": "https://up.example/mcp", "scopes": ""}
        ctx = host._build_oauth_auth(oauth).context
        # Refresh omits expires_in but carries a refresh token -> in-memory expiry gets the
        # default TTL, so is_token_valid() won't treat the token as valid forever (which would
        # skip proactive refresh and 401 into the headless-impossible interactive path).
        before = time.time()
        ctx.update_token_expiry(OAuthToken(access_token="A2", token_type="Bearer", refresh_token="R2"))
        assert ctx.token_expiry_time is not None
        assert before + _DEFAULT_REFRESHABLE_TTL - 5 <= ctx.token_expiry_time <= time.time() + _DEFAULT_REFRESHABLE_TTL + 5
        # expires_in present -> the real value is used.
        ctx.update_token_expiry(OAuthToken(access_token="A3", token_type="Bearer", expires_in=60))
        assert ctx.token_expiry_time <= time.time() + 61
        # No refresh token AND no expires_in -> None (genuinely long-lived; keep sending it).
        ctx.update_token_expiry(OAuthToken(access_token="A4", token_type="Bearer"))
        assert ctx.token_expiry_time is None
    finally:
        store.clear()


def test_bridge_transport_passes_oauth_auth(monkeypatch):
    import anyio

    from app.bridge import host

    store = ServerTokenStorage("srv-bridge-2")
    store.clear()
    anyio.run(store.set_tokens, OAuthToken(access_token="AT", token_type="Bearer"))
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
    try:
        host._build_transport(spec)
        from mcp.client.auth import OAuthClientProvider

        assert isinstance(captured["auth"], OAuthClientProvider)
    finally:
        store.clear()


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


def test_disconnect_non_oauth_server_is_400():
    with TestClient(app) as c:
        r = c.post(
            "/api/servers",
            json={"name": "plain", "runner": "remote", "command": "https://up.example/mcp"},
            headers=LOOPBACK,
        )
        sid = r.json()["id"]
        try:
            resp = c.post(f"/api/servers/{sid}/oauth/disconnect", headers=LOOPBACK)
            assert resp.status_code == 400
            assert "oauth" in resp.json()["detail"].lower()
        finally:
            c.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_delete_server_clears_oauth_tokens():
    with TestClient(app) as c:
        sid = _create_oauth_server(c)
        import anyio

        store = ServerTokenStorage(sid)
        anyio.run(store.set_tokens, OAuthToken(access_token="AT", token_type="Bearer"))
        assert store.path.exists()

        c.delete(f"/api/servers/{sid}", headers=LOOPBACK)
        assert not store.path.exists()
