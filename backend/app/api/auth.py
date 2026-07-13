"""Public control-plane auth status, so the SPA can decide whether to show login
instead of guessing from 401s. Reachable through the Host/Origin allowlist only;
it carries no secrets and reflects the current request's own credential.

Also hosts the upstream-OAuth redirect callback. It must be PUBLIC — it's a
top-level browser navigation initiated by the upstream authorization server, so it
carries no control-plane bearer token. The unguessable ``state`` (bound to the
authorization the operator themselves started) is the security anchor; an unknown
state is simply rejected."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlmodel import Session

from app.api.schemas import AuthStatus
from app.auth import oauth_flow
from app.auth.control_plane import control_auth, enforcement_enabled
from app.db import get_session, repo

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    return AuthStatus(
        enforced=enforcement_enabled(session),
        authenticated=control_auth(request, session) == "ok",
    )


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
        logger.info("OAuth callback returned an error: %s", error_description or error)
        return _oauth_redirect(_ERROR_REDIRECT)
    if not code or not state:
        return _oauth_redirect(_ERROR_REDIRECT)

    try:
        server_id = await oauth_flow.complete_authorization(state, code)
    except KeyError:
        return _oauth_redirect(_ERROR_REDIRECT)
    except Exception as exc:  # token exchange / provider error
        logger.info("OAuth callback failed: %s", exc)
        return _oauth_redirect(_ERROR_REDIRECT)

    # Look the server up by the id the flow reported. Redirecting with the *stored*
    # slug (read from the DB row, never from the request) keeps remote-controlled data
    # out of the Location entirely.
    server = repo.get_server(session, server_id)
    if server is None:
        return _oauth_redirect(_ERROR_REDIRECT)

    # Restart an enabled server so its bridge re-reads the freshly stored tokens
    # (config_hash is unchanged — authenticating never rewrites the row — so the
    # reconciler wouldn't otherwise bounce it). Best-effort; the reconciler brings it
    # back on the next tick regardless.
    if server.enabled:
        sup = request.app.state.supervisor
        try:
            await sup.stop(server.id)
        except Exception:  # noqa: BLE001 — never fail the redirect on a restart hiccup
            logger.debug("could not restart %s after OAuth", server.id, exc_info=True)
        sup.nudge()

    return _oauth_redirect(f"/server/{server.id}?oauth=connected")
