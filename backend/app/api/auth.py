"""Public control-plane auth status, so the SPA can decide whether to show login
instead of guessing from 401s. Reachable through the Host/Origin allowlist only;
it carries no secrets and reflects the current request's own credential.

Also hosts the upstream-OAuth redirect callback. It must be PUBLIC — it's a
top-level browser navigation initiated by the upstream authorization server, so it
carries no control-plane bearer token. The unguessable ``state`` (bound to the
authorization the operator themselves started) is the security anchor; an unknown
state is simply rejected."""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlmodel import Session

from app.api.schemas import AuthStatus, AuthUser
from app.api.util import oauth_public_base
from app.auth import oauth_flow, principal as principal_mod
from app.auth.control_plane import control_auth, enforcement_enabled
from app.db import get_session, repo

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    # ``user`` reflects the request's PRINCIPAL, which exists even when
    # ``authenticated`` is false with enforcement off (the synthetic local admin) —
    # that's what lets the zero-config SPA render the full admin surface.
    p = principal_mod.resolve(request, session)
    user = (
        AuthUser(id=p.user_id, name=p.name, role=p.role, local_runners=p.local_runners)
        if p is not None
        else None
    )
    return AuthStatus(
        enforced=enforcement_enabled(session),
        authenticated=control_auth(request, session) == "ok",
        user=user,
    )


@router.get("/oauth/client-metadata.json")
async def oauth_client_metadata(request: Request) -> dict:
    """This instance's OAuth Client ID Metadata Document (CIMD).

    Providers that support URL-based client ids (the MCP 2025-11-25 authorization
    spec's successor to Dynamic Client Registration) fetch this document server-side
    and use its URL as the client_id — no registration step at all. It must be PUBLIC
    (the provider holds no credential for us) and is harmless to expose: every value
    is already derivable from the instance's own base URL. CIMD clients are public
    clients — PKCE secures the exchange, so ``token_endpoint_auth_method`` is ``none``
    and no secret exists. The flow only offers this URL to a provider when the base is
    https (``oauth_flow._client_metadata_url``); a LAN-only http instance serves the
    document but can never be fetched by a provider, which is inert."""
    base = oauth_public_base(request)
    return {
        "client_id": f"{base}/api/oauth/client-metadata.json",
        "client_name": "mcpelevator",
        "client_uri": base,
        "redirect_uris": [f"{base}/api/oauth/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


# Fixed, literal redirect targets. The callback deliberately puts NO request-derived
# data into the Location header: the SPA reads the coarse ``oauth`` flag and shows its
# own message. Keeping the redirect free of remote input rules out URL-redirection /
# header-injection entirely (the specific failure reason is logged server-side instead).
_ERROR_REDIRECT = "/?oauth=error"


def _oauth_redirect(path: str) -> RedirectResponse:
    # 303 so the browser follows with GET regardless of how it arrived here.
    return RedirectResponse(path, status_code=303)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    session: Session = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Finish an upstream-OAuth sign-in: exchange the code for tokens (in the parked
    flow started by ``/api/servers/{id}/oauth/authorize``) and bounce the operator
    back to the server page. On success the server, if enabled, is restarted so the
    bridge immediately picks up the new tokens.

    Every redirect target here is a fixed literal — no query/param value is echoed into
    the Location — so a malicious ``?error=``/``?state=`` can't turn this into an open
    redirect. The exact failure reason is logged, not reflected."""
    if error:
        logger.info("OAuth callback returned an error: %r", error_description or error)
        return _oauth_redirect(_ERROR_REDIRECT)
    if not code or not state:
        return _oauth_redirect(_ERROR_REDIRECT)

    sup = request.app.state.supervisor
    # Stop the target server's bridge BEFORE the grant is promoted (which happens inside
    # complete_authorization). A running bridge re-authenticating could otherwise refresh
    # its old token and overwrite the just-obtained grant. Stop unconditionally when we can
    # identify the pending server (sup.stop is idempotent — a no-op if nothing is running):
    # gating on ``enabled`` would miss a bridge that is still winding down from a just-
    # toggled server. We bring it back below — with the new tokens on success, or the
    # preserved old ones on failure (the flow leaves the store untouched when it aborts).
    hinted_id = oauth_flow.pending_server_id(state)
    stopped = False
    if hinted_id is not None:
        with contextlib.suppress(Exception):
            await sup.stop(hinted_id)
        stopped = True

    try:
        server_id = await oauth_flow.complete_authorization(state, code)
    except KeyError:
        if stopped:
            sup.nudge()  # unknown state; bring the stopped bridge back with existing tokens
        return _oauth_redirect(_ERROR_REDIRECT)
    except Exception as exc:  # token exchange / provider error
        logger.info("OAuth callback failed: %r", exc)
        if stopped:
            sup.nudge()  # failed re-auth; restart with the preserved old credentials
        return _oauth_redirect(_ERROR_REDIRECT)

    # Look the server up by the id the flow reported. Redirecting with the *stored* id
    # (read from the DB row, never from the request) keeps remote-controlled data out of
    # the Location entirely.
    server = repo.get_server(session, server_id)
    if server is None:
        if stopped:
            sup.nudge()
        return _oauth_redirect(_ERROR_REDIRECT)

    # Force a post-promote restart: stop (idempotent) AFTER the tokens are stored, then
    # nudge. This guarantees the bridge that ends up serving an enabled server is one that
    # STARTED after the promote — closing the window where a bridge spawned before the
    # promote (e.g. resurrected by a racing reconcile) keeps serving stale in-memory
    # tokens. config_hash is unchanged (authenticating never rewrites the row), so without
    # this the reconciler wouldn't bounce it on its own.
    if server.enabled:
        with contextlib.suppress(Exception):
            await sup.stop(server.id)
        sup.request_activation(server.id)

    return _oauth_redirect(f"/server/{server.id}?oauth=connected")
