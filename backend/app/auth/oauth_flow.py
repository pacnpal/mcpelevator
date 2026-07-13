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
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyHttpUrl

from app.auth.oauth_store import ServerTokenStorage
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


def _extract_state(url: str) -> Optional[str]:
    try:
        values = parse_qs(urlparse(url).query).get("state")
    except ValueError:
        return None
    return values[0] if values else None


async def _drive(server, provider: OAuthClientProvider, storage: ServerTokenStorage, pending: _Pending) -> None:
    """Run the provider end to end: one authenticated request that 401s, triggering
    discovery → registration → (park for browser) → code exchange → token storage."""
    mcp_url = server.command
    inner_error: Optional[BaseException] = None
    try:
        async with httpx.AsyncClient(
            auth=provider, timeout=httpx.Timeout(30.0), follow_redirects=True
        ) as client:
            await client.post(mcp_url, json=_INIT_BODY, headers=_INIT_HEADERS)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — tolerate; success is decided by whether tokens landed
        inner_error = exc

    # The exchange stores tokens *before* the original request is retried, so the
    # handshake can succeed even if that retry (or the connection) then errors. Judge
    # success on whether tokens actually landed, not on the request outcome.
    tokens = await storage.get_tokens()
    if tokens is not None:
        # Persist the discovered auth-server metadata so the bridge can refresh
        # against the real token endpoint without repeating discovery.
        meta = getattr(provider.context, "oauth_metadata", None)
        if meta is not None:
            try:
                storage.set_metadata(meta)
            except Exception:  # noqa: BLE001 — metadata is a refresh optimization, not required
                logger.debug("could not persist OAuth metadata for %s", server.id, exc_info=True)
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

    storage = ServerTokenStorage(server.id)
    redirect_uris = [AnyHttpUrl(callback_url)]

    # A static, pre-registered client (operator supplied a client id) skips Dynamic
    # Client Registration: seed the client info into storage so the provider uses it.
    if server.oauth_client_id:
        secret = server.oauth_client_secret or None
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id=server.oauth_client_id,
                client_secret=secret,
                redirect_uris=redirect_uris,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post" if secret else "none",
                scope=server.oauth_scopes or None,
            )
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

    provider = OAuthClientProvider(
        server_url=server.command,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=_FLOW_TIMEOUT,
    )

    _PENDING[pending.id] = pending
    pending.task = asyncio.create_task(_drive(server, provider, storage, pending))

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
