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
from urllib.parse import quote

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
    bridge immediately picks up the new tokens."""
    if error:
        reason = error_description or error
        return _oauth_redirect(f"/?oauth=error&reason={quote(reason)}")
    if not code or not state:
        return _oauth_redirect("/?oauth=error&reason=missing+code+or+state")

    try:
        server_id = await oauth_flow.complete_authorization(state, code)
    except KeyError:
        return _oauth_redirect("/?oauth=error&reason=unknown+or+expired+request")
    except Exception as exc:  # token exchange / provider error
        logger.info("OAuth callback failed: %s", exc)
        return _oauth_redirect(f"/?oauth=error&reason={quote(str(exc)[:200])}")

    # Restart an enabled server so its bridge re-reads the freshly stored tokens
    # (config_hash is unchanged — authenticating never rewrites the row — so the
    # reconciler wouldn't otherwise bounce it). Best-effort; the reconciler brings it
    # back on the next tick regardless.
    server = repo.get_server(session, server_id)
    if server is not None and server.enabled:
        sup = request.app.state.supervisor
        try:
            await sup.stop(server_id)
        except Exception:  # noqa: BLE001 — never fail the redirect on a restart hiccup
            logger.debug("could not restart %s after OAuth", server_id, exc_info=True)
        sup.nudge()

    return _oauth_redirect(f"/server/{quote(server_id)}?oauth=connected")
