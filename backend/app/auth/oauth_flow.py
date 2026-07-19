"""Control-plane OAuth authorization-code flow for remote upstreams.

Getting the *first* set of tokens for an OAuth-protected upstream is interactive:
the operator has to sign in at the provider in a browser. That can't happen inside
the headless bridge subprocess, so it happens here, in the control plane, which has
a UI and a public callback URL.

We drive the MCP SDK's ``OAuthClientProvider`` — which already implements discovery
(RFC 8414 / SEP-985), Dynamic Client Registration, PKCE, the code exchange, and
RFC 8707 resource binding — rather than reimplementing OAuth by hand. The provider
expects a ``redirect_handler`` (given the authorization URL) and a
``callback_handler`` (returns the ``(code, state)`` from the redirect). A desktop
client blocks a local browser + loopback server between the two; we instead split
them across two HTTP requests:

* ``begin_authorization`` starts the provider in a background task and returns the
  authorization URL the moment the provider produces it — the SPA sends the browser
  there.
* The provider then parks in ``callback_handler`` until the upstream redirects the
  browser back to ``/api/oauth/callback``; ``complete_authorization`` feeds the
  ``(code, state)`` in, the background task finishes the exchange, and the tokens
  land in the shared :class:`~app.auth.oauth_store.ServerTokenStorage` file — where
  the bridge picks them up (and refreshes them) from then on.

State is single-process, in-memory (this is a single-worker uvicorn); a background
reaper drops entries the operator never completed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import httpx
from mcp.client.auth import OAuthClientProvider, OAuthRegistrationError, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyHttpUrl

from app.auth.oauth_store import ServerTokenStorage
from app.runners.remote import DEFAULT_TRANSPORT, canonical_transport
from app.util import new_id

logger = logging.getLogger(__name__)

CLIENT_NAME = "mcpelevator"
# SEP-2207 (accepted 2026): an OAuth client that wants a refresh token keeps
# ``refresh_token`` in its grant_types (we do) AND requests the ``offline_access``
# scope. Most authorization servers only mint a refresh token when the client asks
# for offline access, so without this every remote-OAuth session lapses on the short
# access-token clock and the operator has to re-authenticate by hand.
OFFLINE_ACCESS = "offline_access"
# Seconds to obtain the authorization URL (metadata discovery + client registration).
_URL_TIMEOUT = 30.0
# Seconds the operator has to complete the browser sign-in before we give up.
_FLOW_TIMEOUT = 600.0

# A minimal MCP initialize call — just enough of a real request to make the upstream
# answer 401 and hand us the ``WWW-Authenticate`` that kicks off the OAuth handshake.
_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": "oauth-setup"},
    },
}
_INIT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


class OAuthBeginError(RuntimeError):
    """A failure to START the interactive OAuth flow, carrying an operator-facing message
    and the HTTP status the API should answer with. Lets ``begin_authorization`` translate
    a raw SDK/provider error into something actionable before it reaches the route handler,
    instead of the route dumping the provider's raw JSON body as an opaque 502."""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _registration_status(exc: OAuthRegistrationError) -> Optional[int]:
    """Pull the HTTP status out of an ``OAuthRegistrationError``. The SDK bakes it into the
    message as ``"Registration failed: <status> <body>"`` (mcp.client.auth.utils) with no
    structured field, so the message is the only place to read it back from."""
    match = re.match(r"Registration failed: (\d{3})\b", str(exc))
    return int(match.group(1)) if match else None


def _classify_begin_error(exc: BaseException) -> OAuthBeginError:
    """Translate a raw begin-flow failure into an operator-facing :class:`OAuthBeginError`.

    The common actionable case is the upstream rate-limiting Dynamic Client Registration
    (HTTP 429): surface that as a clean 429 with next steps, rather than a 502 carrying the
    provider's raw error JSON. Everything else keeps the previous generic message + 502."""
    if isinstance(exc, OAuthBeginError):
        return exc
    status = _registration_status(exc) if isinstance(exc, OAuthRegistrationError) else None
    if status == 429:
        return OAuthBeginError(
            "the OAuth provider is rate-limiting Dynamic Client Registration (HTTP 429). "
            "Wait a minute and try connecting again; if it keeps happening, register a client "
            "with the provider and set an explicit Client ID to skip registration.",
            status_code=429,
        )
    return OAuthBeginError(f"could not start OAuth: {exc}", status_code=502)


def _merge_scopes(*scope_strings: Optional[str]) -> Optional[str]:
    """Union space-delimited scope strings, preserving first-seen order and dropping
    duplicates. Returns ``None`` when nothing was supplied (so the SDK omits ``scope``)."""
    merged = dict.fromkeys(s for ss in scope_strings for s in (ss or "").split())
    return " ".join(merged) if merged else None


def _offline_access_default(context) -> bool:
    """Whether to add ``offline_access`` (→ refresh token) to the requested scope.

    SEP-2207: request it by default, UNLESS the authorization server publishes a
    ``scopes_supported`` list that omits it. That exception respects an AS which
    validates scopes strictly — an unadvertised ``offline_access`` would otherwise get
    the whole authorization rejected with ``invalid_scope``. When the AS advertises it,
    or publishes no scope list at all, we ask, so the common case (short-lived access
    token + long-lived refresh token) works with no operator action. An operator whose
    provider honours ``offline_access`` without advertising it can still type it into
    the scopes field — operator scopes are always requested, gate or no gate.
    """
    metadata = context.oauth_metadata
    supported = metadata.scopes_supported if metadata else None
    if supported is None:
        return True
    return OFFLINE_ACCESS in supported


class _ScopedOAuthClientProvider(OAuthClientProvider):
    """``OAuthClientProvider`` that requests a refresh token and keeps the operator's scopes.

    The SDK runs its own "scope selection strategy" during the 401-driven handshake and
    OVERWRITES ``context.client_metadata.scope`` from the WWW-Authenticate header / the
    discovered resource+auth-server metadata (``oauth2.get_client_metadata_scopes``),
    discarding whatever the operator typed. That's wrong when the operator deliberately
    asked for a specific set (e.g. an upstream that doesn't advertise scopes, or one
    that needs a scope it omits from the challenge). Immediately before the authorization
    URL is built we union back in, so they're always requested while still honouring any
    the server volunteered: (1) ``offline_access`` by default (SEP-2207 — see
    ``_offline_access_default``), so the provider issues a refresh token and the session
    doesn't lapse on the access-token clock, and (2) the operator's explicit scopes."""

    def __init__(self, *args, operator_scopes: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._operator_scopes = operator_scopes or None

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        extra = self._operator_scopes
        if _offline_access_default(self.context):
            extra = _merge_scopes(extra, OFFLINE_ACCESS)
        if extra:
            self.context.client_metadata.scope = _merge_scopes(
                self.context.client_metadata.scope, extra
            )
        return await super()._perform_authorization_code_grant()


class _MemoryTokenStorage(TokenStorage):
    """Ephemeral, in-process token storage used to DRIVE one interactive grant.

    The flow runs against this instead of the shared file store so that an existing,
    working credential is never destroyed by a re-authorization that then fails or is
    cancelled: the real store is written only on success (see ``_drive``'s promotion).
    Seeded with the existing client info so a re-auth reuses the registered client and
    a static-client grant skips Dynamic Client Registration. Also forces the probe to
    401 (no tokens here), which is what triggers the browser redirect."""

    def __init__(
        self,
        client_info: Optional[OAuthClientInformationFull] = None,
        *,
        persist_registration_to: Optional[ServerTokenStorage] = None,
    ):
        self._tokens: Optional[OAuthToken] = None
        self._client_info = client_info
        # When set, a client the SDK newly REGISTERS mid-flow is written straight through to
        # the shared store (client_info only, never tokens). Wired up by begin_authorization
        # solely in the DCR path when the real store holds no tokens, so an abandoned or failed
        # browser step doesn't discard the registration and force the next sign-in to register
        # again (burning the provider's registration quota).
        self._persist_registration_to = persist_registration_to

    async def get_tokens(self) -> Optional[OAuthToken]:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info
        if self._persist_registration_to is not None:
            # set_client_info touches only the client_info key of the file, leaving any tokens
            # untouched — and this is only wired when there were no tokens to begin with, so it
            # can never rebind a client that a live credential's refresh depends on.
            await self._persist_registration_to.set_client_info(client_info)


class _Pending:
    """One in-flight authorization the operator has started but not yet completed."""

    def __init__(self, server_id: str):
        loop = asyncio.get_running_loop()
        self.id = new_id()
        self.server_id = server_id
        self.state: Optional[str] = None  # OAuth ``state``, learned from the auth URL
        self.code: Optional[str] = None  # filled in by the callback
        self.url_future: asyncio.Future[str] = loop.create_future()
        self.done_future: asyncio.Future[None] = loop.create_future()
        self.callback_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.created_at = time.monotonic()


# id -> pending, plus a state -> id index (state is only known once the provider
# yields the authorization URL, i.e. after discovery/registration succeed).
_PENDING: dict[str, _Pending] = {}
_STATE_INDEX: dict[str, str] = {}


def _forget(pending: _Pending) -> None:
    _PENDING.pop(pending.id, None)
    if pending.state is not None:
        _STATE_INDEX.pop(pending.state, None)
    if pending.task is not None and not pending.task.done():
        pending.task.cancel()


def _reap_stale() -> None:
    now = time.monotonic()
    for pending in list(_PENDING.values()):
        if now - pending.created_at > _FLOW_TIMEOUT:
            _forget(pending)


def _cancel_existing(server_id: str) -> None:
    """Only one authorization can be in flight per server — a second click supersedes
    the first (its state/PKCE would otherwise dangle until it reaps)."""
    for pending in list(_PENDING.values()):
        if pending.server_id == server_id:
            _forget(pending)


def pending_server_id(state: str) -> Optional[str]:
    """The server id of the authorization parked on ``state``, or ``None`` — so the
    callback can stop that server's running bridge *before* the grant is promoted,
    closing the window where an old bridge's refresh could overwrite the new tokens."""
    pending_id = _STATE_INDEX.get(state)
    pending = _PENDING.get(pending_id) if pending_id else None
    return pending.server_id if pending is not None else None


def cancel_pending(server_id: str) -> None:
    """Cancel any in-flight authorization for a server. Called when its OAuth config is
    edited: a background flow started against the OLD upstream/scopes/client must not
    complete and write credentials for the wrong resource back under this id."""
    _cancel_existing(server_id)


def _repair_authorization_url(url: str) -> str:
    """Fix authorization URLs the SDK builds for endpoints that already carry a query.

    The SDK joins its parameters as ``f"{auth_endpoint}?{urlencode(params)}"`` — but some
    providers advertise an ``authorization_endpoint`` that itself contains a query string
    (Railway: ``/oauth/auth?resource=https%3A%2F%2Fbackboard.railway.com``). The blind join
    yields a second raw ``?``, and everything after it — ``response_type=code`` first —
    is swallowed into the preceding parameter's value, so the provider rejects the request.

    RFC 3986 permits raw ``?`` inside a query, so the endpoint's own query may legally
    contain more of them — but ``urlencode`` percent-encodes ``?``, so in the joined URL
    the LAST raw ``?`` is always the separator the SDK added. Re-join only that one with
    ``&``, leaving the endpoint's own query byte-for-byte intact."""
    base, sep, params = url.rpartition("?")
    if not sep or "?" not in base:
        return url  # zero or one '?' — already well-formed
    return f"{base}&{params}"


def _ensure_consent_prompt(url: str) -> str:
    """Add ``prompt=consent`` to an authorization URL that requests ``offline_access``.

    OIDC providers only (re)issue a refresh token for offline access when the user actively
    consents, which the spec ties to ``prompt=consent``. Without it a returning, already-
    consented user gets an authorization code but NO refresh token — the very lapse
    requesting ``offline_access`` is meant to avoid (SEP-2207). The SDK's URL builder
    serializes only the standard params plus ``scope``, so we append it here.

    Skipped when the URL already carries a ``prompt`` (don't clobber a provider-specific
    value) or doesn't request offline access. A non-OIDC OAuth2 server simply ignores the
    unknown parameter (RFC 6749 §3.1), so sending it for offline-access requests is safe.
    The existing query is preserved byte-for-byte (see ``_repair_authorization_url``) — only
    the new parameter is appended — and any parse hiccup on an exotic URL just leaves it
    unchanged rather than risking corruption."""
    parts = urlsplit(url)
    try:
        query = parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return url
    scope = " ".join(query.get("scope", [])).split()
    if OFFLINE_ACCESS not in scope or query.get("prompt"):
        return url
    # Reached only with offline_access in the scope param, so the query is non-empty;
    # "prompt=consent" needs no escaping, so append it directly.
    new_query = f"{parts.query}&prompt=consent"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _extract_state(url: str) -> Optional[str]:
    try:
        values = parse_qs(urlparse(url).query).get("state")
    except ValueError:
        return None
    return values[0] if values else None


def _probe_headers(server) -> dict[str, str]:
    """Headers for the probe request: the base MCP headers plus the server's own extra
    headers — but NEVER a stale ``Authorization``. If the server was switched from static
    Headers auth to OAuth, a leftover token header would make the upstream answer 200
    instead of the 401 that drives the OAuth flow, hanging begin() into a timeout."""
    headers = dict(_INIT_HEADERS)
    for key, value in (server.env or {}).items():
        if key.strip().lower() == "authorization":
            continue
        headers[key] = value
    return headers


async def _drive(
    server,
    provider: OAuthClientProvider,
    mem: _MemoryTokenStorage,
    real: ServerTokenStorage,
    pending: _Pending,
) -> None:
    """Run the provider end to end: one authenticated request that 401s, triggering
    discovery → registration → (park for browser) → code exchange. Tokens land in the
    ephemeral ``mem`` store; only on success are they PROMOTED to the shared ``real``
    store, so a failed re-auth never destroys a still-working credential."""
    mcp_url = server.command
    # Probe with the SAME transport the bridge will use so an SSE upstream reaches its
    # 401/auth challenge here instead of failing before the redirect.
    transport = canonical_transport((server.args or [None])[0]) or DEFAULT_TRANSPORT
    headers = _probe_headers(server)
    method, kwargs = ("GET", {}) if transport == "sse" else ("POST", {"json": _INIT_BODY})
    inner_error: Optional[BaseException] = None
    final_status: Optional[int] = None
    try:
        async with httpx.AsyncClient(
            auth=provider, timeout=httpx.Timeout(30.0), follow_redirects=True
        ) as client:
            # STREAM (don't read the body): the OAuth handshake runs to completion before this
            # final response is returned, so the body is irrelevant — and an SSE upstream keeps
            # its ``text/event-stream`` response open (heartbeats), which a body-reading
            # ``get()`` would block on forever, hanging the flow past its timeout. We DO read the
            # status line (available once headers arrive, without touching the body) to tell a
            # genuinely usable grant from one the resource still rejects.
            async with client.stream(method, mcp_url, headers=headers, **kwargs) as response:
                final_status = response.status_code
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — tolerate; success is decided by whether tokens landed
        inner_error = exc

    # The exchange stores tokens *before* the original request is retried, so the
    # handshake can succeed even if that retry (or the connection) then errors. Judge
    # success on whether tokens actually landed in the ephemeral store.
    tokens = await mem.get_tokens()
    # ...but if the RETRIED MCP request still came back 401/403, the resource rejected the new
    # token (bound to the wrong resource, missing a required scope, etc.). Promoting it would
    # leave the UI reporting "connected" and restart the bridge with a credential the upstream
    # refuses, so treat that as an authorization failure instead of a success.
    if tokens is not None and final_status in (401, 403):
        inner_error = inner_error or RuntimeError(
            f"the upstream still rejected the new OAuth token (HTTP {final_status}) — the "
            "granted scopes or resource may not match what this server requires"
        )
        tokens = None
    if tokens is not None:
        # Promote the freshly-obtained credentials to the shared store the bridge reads, in
        # ONE atomic write that fully replaces any prior state. Building it in memory first
        # means a failure leaves the previous (still-working) credential intact — no
        # destructive pre-clear — and it doesn't carry forward an old refresh token (a new
        # grant brings its own; the carry-forward is only for the bridge's refresh path).
        try:
            real.promote(
                tokens=tokens,
                client_info=await mem.get_client_info(),
                metadata=getattr(provider.context, "oauth_metadata", None),
                protected_resource_metadata=getattr(
                    provider.context, "protected_resource_metadata", None
                ),
            )
        except Exception as exc:  # noqa: BLE001 — persistence failure = the grant didn't stick
            inner_error = exc
        else:
            if not pending.done_future.done():
                pending.done_future.set_result(None)
            return

    error = inner_error or RuntimeError("OAuth flow finished without returning tokens")
    logger.info("OAuth authorization for %s failed: %s", server.id, error)
    url_pending = not pending.url_future.done()
    if url_pending:
        # Failed during discovery/registration, before an authorization URL was produced
        # (e.g. the upstream rate-limited DCR with a 429): begin_authorization is awaiting
        # url_future and will surface this to the operator.
        pending.url_future.set_exception(error)
    if not pending.done_future.done():
        if url_pending or not pending.callback_event.is_set():
            # done_future is only ever awaited by complete_authorization, which runs only after
            # a URL was returned AND the browser came back (callback_event set). Having failed
            # before the URL, or after it but with the browser never returning (the callback
            # wait timed out), no one will ever retrieve done_future's exception — setting one
            # would log a spurious "Future exception was never retrieved" when it's collected. A
            # cancelled, never-awaited future is silent, so cancel it instead.
            pending.done_future.cancel()
        else:
            # A URL was handed back and the callback arrived; complete_authorization is awaiting
            # done_future, so surface the failure there.
            pending.done_future.set_exception(error)


async def begin_authorization(server, *, callback_url: str) -> str:
    """Start the interactive flow for ``server`` and return the URL to send the
    operator's browser to. Raises on a discovery/registration failure or timeout."""
    _reap_stale()
    _cancel_existing(server.id)

    real = ServerTokenStorage(server.id)
    redirect_uris = [AnyHttpUrl(callback_url)]

    # Seed the client info the flow will use. A static, pre-registered client (operator
    # supplied a client id) skips Dynamic Client Registration; otherwise reuse a prior DCR
    # registration if one exists so a re-auth doesn't register a brand-new client each time.
    if server.oauth_client_id:
        secret = server.oauth_client_secret or None
        seed_client_info: Optional[OAuthClientInformationFull] = OAuthClientInformationFull(
            client_id=server.oauth_client_id,
            client_secret=secret,
            redirect_uris=redirect_uris,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if secret else "none",
            scope=server.oauth_scopes or None,
        )
    else:
        # Reuse a prior DCR registration only if it's still usable. Force re-registration
        # when: it was registered for a DIFFERENT callback URL (mcpelevator now reached via a
        # different base URL — localhost vs LAN, or a changed MCPE_PUBLIC_BASE_URL — whose
        # redirect_uri a strict provider would reject), OR its client secret has EXPIRED
        # (a past nonzero client_secret_expires_at), which would otherwise fail the exchange
        # and leave the operator unable to reconnect via Re-authenticate.
        existing = await real.get_client_info()
        registered = {str(u) for u in (getattr(existing, "redirect_uris", None) or [])}
        expires_at = getattr(existing, "client_secret_expires_at", None) if existing else None
        expired = bool(expires_at) and expires_at < time.time()
        reusable = existing is not None and callback_url in registered and not expired
        seed_client_info = existing if reusable else None

    # Drive the grant against an EPHEMERAL store (no tokens → the probe 401s → the browser
    # redirect fires). The shared store is written only if the grant succeeds (_drive), so a
    # failed or cancelled re-auth can't wipe a still-working credential. The one exception is a
    # freshly DCR-registered client when no seed and no stored tokens exist: persist that
    # registration straight through, so an abandoned browser step doesn't force a re-register
    # next time (quota). Guarded on "no tokens" so it can never rebind a client a live token
    # depends on.
    persist_registration_to = (
        real if seed_client_info is None and (await real.get_tokens()) is None else None
    )
    mem = _MemoryTokenStorage(
        client_info=seed_client_info, persist_registration_to=persist_registration_to
    )

    client_metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=redirect_uris,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=server.oauth_scopes or None,
    )

    pending = _Pending(server.id)

    async def redirect_handler(authorization_url: str) -> None:
        authorization_url = _repair_authorization_url(authorization_url)
        authorization_url = _ensure_consent_prompt(authorization_url)
        state = _extract_state(authorization_url)
        pending.state = state
        if state is not None:
            _STATE_INDEX[state] = pending.id
        if not pending.url_future.done():
            pending.url_future.set_result(authorization_url)

    async def callback_handler() -> tuple[str, Optional[str]]:
        try:
            await asyncio.wait_for(pending.callback_event.wait(), timeout=_FLOW_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("timed out waiting for the operator to finish signing in") from exc
        if pending.code is None:
            raise RuntimeError("OAuth callback delivered no authorization code")
        return pending.code, pending.state

    provider = _ScopedOAuthClientProvider(
        server_url=server.command,
        client_metadata=client_metadata,
        storage=mem,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=_FLOW_TIMEOUT,
        operator_scopes=server.oauth_scopes or None,
    )

    _PENDING[pending.id] = pending
    pending.task = asyncio.create_task(_drive(server, provider, mem, real, pending))

    try:
        return await asyncio.wait_for(asyncio.shield(pending.url_future), timeout=_URL_TIMEOUT)
    except asyncio.TimeoutError as exc:
        _forget(pending)
        raise OAuthBeginError(
            "timed out contacting the OAuth provider (metadata discovery / registration)"
        ) from exc
    except Exception as exc:
        _forget(pending)
        raise _classify_begin_error(exc) from exc


async def complete_authorization(state: str, code: str) -> str:
    """Feed the callback's ``(code, state)`` into the parked flow and wait for it to
    finish. Returns the server id on success. Raises ``KeyError`` for an unknown/expired
    state, or the underlying error if the token exchange fails."""
    _reap_stale()
    pending_id = _STATE_INDEX.get(state)
    pending = _PENDING.get(pending_id) if pending_id else None
    if pending is None:
        raise KeyError("unknown or expired OAuth state")
    pending.code = code
    pending.callback_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(pending.done_future), timeout=90.0)
    finally:
        _forget(pending)
    return pending.server_id
