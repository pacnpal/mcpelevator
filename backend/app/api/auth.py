"""Public control-plane auth status, so the SPA can decide whether to show login
instead of guessing from 401s. Reachable through the Host/Origin allowlist only;
it carries no secrets and reflects the current request's own credential.

Also hosts the upstream-OAuth redirect callback. It must be PUBLIC — it's a
top-level browser navigation initiated by the upstream authorization server, so it
carries no control-plane bearer token. The unguessable ``state`` (bound to the
authorization the operator themselves started) is the security anchor; an unknown
state is simply rejected."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback

import httpx
from fastapi import APIRouter, Depends
from mcp.client.auth import OAuthTokenError
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
# header-injection entirely (the full failure reason is logged server-side instead).
_ERROR_REDIRECT = "/?oauth=error"

# Coarse failure codes appended as ``&reason=`` so the SPA's toast can say something
# better than "OAuth sign-in failed." (it already renders `OAuth failed: <reason>`).
# Every value is a LITERAL authored here — never a provider-supplied string — so the
# no-remote-input property of the redirect is preserved; the detail stays in the log.
# Each code names only what we can actually tell apart, so none of them sends the
# operator after the wrong thing:
_REASON_PROVIDER_DENIED = "provider_denied"  # the AS came back with ?error=…
_REASON_NO_CODE = "no_code"  # redirect carried no code/state
_REASON_EXPIRED = "expired_or_superseded"  # unknown state: reaped, cancelled, restarted
_REASON_EXCHANGE_FAILED = "token_exchange_failed"  # the AS refused the code (SDK OAuthTokenError)
_REASON_TOKEN_REFUSED = "upstream_refused_token"  # got a token; the RESOURCE rejected it (scopes)
_REASON_CONFIG_CHANGED = "config_changed"  # server deleted/reassigned/reconfigured mid-flow
_REASON_TIMEOUT = "timed_out"  # the exchange didn't finish inside the callback's budget
_REASON_UNEXPECTED = "unexpected_error"  # anything else — the log carries the detail
_REASON_SERVER_GONE = "server_deleted"  # the row vanished mid-flow

# A sanitized traceback is folded to a single line, so it needs more room than a plain
# message before truncation makes it useless.
_TRACEBACK_LIMIT = 4000


def _oauth_redirect(path: str) -> RedirectResponse:
    # 303 so the browser follows with GET regardless of how it arrived here.
    return RedirectResponse(path, status_code=303)


def _oauth_error(reason: str) -> RedirectResponse:
    """Failure redirect carrying a coarse, self-authored reason code."""
    return _oauth_redirect(f"{_ERROR_REDIRECT}&reason={reason}")


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
        # WARNING, not INFO: an operator-initiated sign-in just failed and this text is
        # the ONLY record of why. Under uvicorn's default logging the root logger has no
        # handler and app.* sits at WARNING, so an INFO line here is discarded outright —
        # the operator would be left with a toast and nothing to debug from.
        #
        # But this endpoint is PUBLIC, so the detail is only echoed once the ``state``
        # correlates with an authorization THIS instance started (the same anchor the
        # success path uses). A real provider denial always echoes that state (RFC 6749
        # §4.1.2.1); an anonymous caller replaying ``?error=…&error_description=…``
        # cannot forge one, so it can never write its own text into the log. Even when
        # correlated the text is sanitized and bounded (``log_safe``) — the upstream is
        # trusted to be the upstream, not to be well-behaved.
        if state and oauth_flow.pending_server_id(state) is not None:
            logger.warning(
                "OAuth callback returned an error: %s",
                oauth_flow.log_safe(error_description or error),
            )
        else:
            logger.warning(
                "OAuth callback reported an error for an unknown or expired state; "
                "ignoring the unsolicited callback (detail withheld: it is not tied to "
                "any sign-in this instance started)"
            )
        return _oauth_error(_REASON_PROVIDER_DENIED)
    if not code or not state:
        logger.warning("OAuth callback arrived without a code/state pair")
        return _oauth_error(_REASON_NO_CODE)

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
        logger.warning(
            "OAuth callback carried an unknown or expired state — the sign-in was "
            "superseded by a newer attempt, timed out, or the control plane restarted "
            "mid-flow. Start the sign-in again."
        )
        if stopped:
            sup.nudge()  # unknown state; bring the stopped bridge back with existing tokens
        return _oauth_error(_REASON_EXPIRED)
    except oauth_flow.OAuthGrantRejected as exc:
        # A token WAS issued; the resource server refused it. Its own code, because the
        # remedy is specific: adjust the server's scopes (or the upstream URL the token
        # is bound to), not retry the sign-in.
        logger.warning(
            "OAuth sign-in produced a token the upstream rejected: %s",
            oauth_flow.log_safe(exc),
        )
        if stopped:
            sup.nudge()
        return _oauth_error(_REASON_TOKEN_REFUSED)
    except oauth_flow.OAuthPromotionBlocked as exc:
        logger.warning("OAuth grant discarded: %s", oauth_flow.log_safe(exc))
        if stopped:
            sup.nudge()
        return _oauth_error(_REASON_CONFIG_CHANGED)
    except OAuthTokenError as exc:
        # The authorization server refused the code itself (bad redirect_uri, expired or
        # replayed code, PKCE mismatch, client auth rejected).
        logger.warning("OAuth token exchange failed: %s", oauth_flow.log_safe(exc))
        if stopped:
            sup.nudge()
        return _oauth_error(_REASON_EXCHANGE_FAILED)
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
        # httpx raises its OWN timeout hierarchy (ReadTimeout/ConnectTimeout/…), which is
        # not a builtin TimeoutError — the flow's client has a 30s budget, so an upstream
        # that stalls is a ROUTINE timeout and must read as one instead of falling through
        # to "unexpected_error".
        logger.warning(
            "OAuth token exchange did not finish in time: %s", oauth_flow.log_safe(exc)
        )
        if stopped:
            sup.nudge()
        return _oauth_error(_REASON_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — deliberate boundary, see below
        # Everything else: a persistence failure writing the token store, or an outright
        # bug. The catch-all is deliberate and must stay — this is a PUBLIC endpoint the
        # provider redirects a browser to, so an escaping exception would strand the
        # operator on an unstyled 500 with the bridge still stopped. Instead they land
        # back in the UI with an honestly UNCLASSIFIED code (never one claiming a phase
        # we didn't identify), the bridge is restored, and the traceback goes to the log.
        # NOT exc_info=True: the formatter would append the ORIGINAL exception and its
        # traceback verbatim, straight past the redaction and the length bound applied to
        # the message argument. The stack is still worth having for an unclassified
        # failure, so it is formatted here and sanitized like any other provider-derived
        # text (folded to one line by the control-character pass, hence the larger bound).
        logger.warning(
            "OAuth callback failed unexpectedly: %s | traceback: %s",
            oauth_flow.log_safe(exc),
            oauth_flow.log_safe(traceback.format_exc(), limit=_TRACEBACK_LIMIT),
        )
        if stopped:
            sup.nudge()  # failed re-auth; restart with the preserved old credentials
        return _oauth_error(_REASON_UNEXPECTED)

    # Look the server up by the id the flow reported. Redirecting with the *stored* id
    # (read from the DB row, never from the request) keeps remote-controlled data out of
    # the Location entirely.
    server = repo.get_server(session, server_id)
    if server is None:
        logger.warning("OAuth sign-in completed for a server that no longer exists")
        if stopped:
            sup.nudge()
        return _oauth_error(_REASON_SERVER_GONE)

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
