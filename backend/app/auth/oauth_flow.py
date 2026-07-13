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
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyHttpUrl

from app.auth.oauth_store import ServerTokenStorage
from app.runners.remote import DEFAULT_TRANSPORT, canonical_transport
from app.util import new_id

logger = logging.getLogger(__name__)

CLIENT_NAME = "mcpelevator"
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


def _merge_scopes(*scope_strings: Optional[str]) -> Optional[str]:
    """Union space-delimited scope strings, preserving first-seen order and dropping
    duplicates. Returns ``None`` when nothing was supplied (so the SDK omits ``scope``)."""
    seen: list[str] = []
    for scope_string in scope_strings:
        for scope in (scope_string or "").split():
            if scope not in seen:
                seen.append(scope)
    return " ".join(seen) if seen else None


class _ScopedOAuthClientProvider(OAuthClientProvider):
    """``OAuthClientProvider`` that keeps the operator's requested scopes in the grant.

    The SDK runs its own "scope selection strategy" during the 401-driven handshake and
    OVERWRITES ``context.client_metadata.scope`` from the WWW-Authenticate header / the
    discovered resource+auth-server metadata (``oauth2.get_client_metadata_scopes``),
    discarding whatever the operator typed. That's wrong when the operator deliberately
    asked for a specific set (e.g. an upstream that doesn't advertise scopes, or one
    that needs a scope it omits from the challenge). We union the operator's scopes back
    in immediately before the authorization URL is built so they're always requested,
    while still honouring any the server volunteered."""

    def __init__(self, *args, operator_scopes: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._operator_scopes = operator_scopes or None

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        if self._operator_scopes:
            self.context.client_metadata.scope = _merge_scopes(
                self.context.client_metadata.scope, self._operator_scopes
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
    try:
        async with httpx.AsyncClient(
            auth=provider, timeout=httpx.Timeout(30.0), follow_redirects=True
        ) as client:
            # STREAM (don't read the body): the OAuth handshake runs to completion before this
            # final response is returned, so the body is irrelevant — and an SSE upstream keeps
            # its ``text/event-stream`` response open (heartbeats), which a body-reading
            # ``get()`` would block on forever, hanging the flow past its timeout.
            async with client.stream(method, mcp_url, headers=headers, **kwargs):
                pass
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — tolerate; success is decided by whether tokens landed
        inner_error = exc

    # The exchange stores tokens *before* the original request is retried, so the
    # handshake can succeed even if that retry (or the connection) then errors. Judge
    # success on whether tokens actually landed in the ephemeral store.
    tokens = await mem.get_tokens()
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
    if not pending.url_future.done():
        pending.url_future.set_exception(error)
    if not pending.done_future.done():
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
        raise RuntimeError(
            "timed out contacting the OAuth provider (metadata discovery / registration)"
        ) from exc
    except Exception:
        _forget(pending)
        raise


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
