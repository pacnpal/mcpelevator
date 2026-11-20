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
import hmac
import json
import logging
import secrets
import time
import traceback
from urllib.parse import urlencode

import httpx2
from fastapi import APIRouter, Depends
from mcp.client.auth import OAuthTokenError
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from app import sealed
from app.api.schemas import AuthStatus, AuthUser, SsoInfo
from app.api.util import oauth_public_base
from app.auth import oauth_flow, oidc, principal as principal_mod, sign_in
from app.auth.control_plane import control_auth, enforcement_enabled
from app.config import get_settings
from app.db import get_session, repo
from app.registry import settings as runtime_settings
from app.util import host_only

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    # ``user`` reflects the request's PRINCIPAL, which exists even when
    # ``authenticated`` is false with enforcement off (the synthetic local admin) —
    # that's what lets the zero-config SPA render the full admin surface.
    p = principal_mod.resolve(request, session)
    user = None
    if p is not None:
        row = repo.get_user(session, p.user_id) if p.user_id else None
        user = AuthUser(
            id=p.user_id, name=p.name, role=p.role, local_runners=p.local_runners,
            linked=bool(row is not None and row.oidc_subject),
        )
    values = runtime_settings.read_all(session)
    # Handed out whenever the cookie names a live session, enforcement or not: with
    # enforcement off the SPA still needs it for the session to count (e.g. as the
    # admin credential that turns enforcement on).
    live = sign_in.current(request, session) if sign_in.accepts(request) else None
    return AuthStatus(
        enforced=enforcement_enabled(session),
        authenticated=control_auth(request, session) == "ok",
        user=user,
        sso=SsoInfo(enabled=True, label=values["oidc_label"] or "SSO")
        if values["oidc_enabled"] else None,
        csrf=sign_in.csrf(live.id) if live is not None else None,
    )


# ---- SSO sign-in (authorization code + PKCE; app.auth.oidc) -------------------

# The flow's state, nonce, PKCE verifier and next path ride in a sealed cookie named per
# state, so nothing is held server-side and two tabs signing in at once don't collide.
_FLOW_COOKIE_PREFIX = "mcpe_oidc_"
_FLOW_PATH = "/api/auth/oidc"
_FLOW_TTL_S = 600


def _flow_cookie(state: str) -> str:
    return _FLOW_COOKIE_PREFIX + state[:16]


def _safe_next(value: str | None) -> str:
    """A same-origin path, else ``/``."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


def _sso_error(reason: str, detail: str, state: str | None = None) -> RedirectResponse:
    """Failure redirect carrying one of oidc's literal reason codes. ``detail`` (which
    may hold provider text) goes to the log only."""
    logger.warning("SSO sign-in failed (%s): %s", reason, oauth_flow.log_safe(detail))
    resp = RedirectResponse(f"/login?sso=error&reason={reason}", status_code=303)
    if state:
        resp.delete_cookie(_flow_cookie(state), path=_FLOW_PATH)
    return resp


def _redirect_uri(request: Request) -> str:
    return f"{oauth_public_base(request)}/api/auth/oidc/callback"


@router.get("/auth/oidc/login")
async def oidc_login(
    request: Request, next: str | None = None, session: Session = Depends(get_session)
):
    target = _safe_next(next)
    config = get_settings()
    if config.public_host and host_only(request.headers.get("host", "")) != config.public_host:
        # Start on the canonical host, so the flow cookie and the session cookie land
        # where the provider sends the browser back to.
        query = urlencode({"next": target})
        return RedirectResponse(f"{config.base_url}/api/auth/oidc/login?{query}", status_code=303)
    try:
        discovery = await oidc.begin(session)
    except oidc.SignInFailed as exc:
        return _sso_error(exc.reason, str(exc))
    values = runtime_settings.read_all(session)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    redirect_uri = _redirect_uri(request)
    resp = RedirectResponse(
        oidc.authorization_url(
            discovery, client_id=values["oidc_client_id"], redirect_uri=redirect_uri,
            scopes=values["oidc_scopes"], state=state, nonce=nonce, verifier=verifier,
        ),
        status_code=303,
    )
    flow = {"state": state, "nonce": nonce, "verifier": verifier, "next": target,
            "exp": time.time() + _FLOW_TTL_S}
    resp.set_cookie(
        _flow_cookie(state), sealed.seal(json.dumps(flow)), max_age=_FLOW_TTL_S,
        path=_FLOW_PATH, secure=redirect_uri.startswith("https://"), httponly=True,
        samesite="lax",
    )
    return resp


def _flow(request: Request, state: str | None) -> dict | None:
    """The sealed flow cookie for ``state``, or None when it's missing, expired or isn't
    this state's."""
    if not state:
        return None
    opened = sealed.unseal(request.cookies.get(_flow_cookie(state), ""))
    try:
        flow = json.loads(opened) if opened else None
    except ValueError:
        return None
    if not isinstance(flow, dict) or flow.get("exp", 0) < time.time():
        return None
    if not hmac.compare_digest(str(flow.get("state", "")).encode(), state.encode()):
        return None
    return flow


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request,
    session: Session = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    iss: str | None = None,
    error: str | None = None,
):
    """Finish an SSO sign-in and start a Sign-in session. Every redirect target is a
    literal or the ``next`` path checked at login: no query value reaches the Location."""
    flow = _flow(request, state)
    if error:
        return _sso_error(oidc.provider_error_reason(error), f"provider error {error!r}", state)
    if flow is None:
        return _sso_error(oidc.EXPIRED, "unknown or expired state", state)
    if not code:
        return _sso_error(oidc.PROVIDER_REJECTED, "callback carried no code", state)
    redirect_uri = _redirect_uri(request)
    try:
        user, id_token = await oidc.finish(
            session, code=code, verifier=flow["verifier"], nonce=flow["nonce"],
            redirect_uri=redirect_uri, iss=iss,
        )
    except oidc.SignInFailed as exc:
        return _sso_error(exc.reason, str(exc), state)
    raw = sign_in.start(session, user.id, id_token)
    resp = RedirectResponse(_safe_next(flow.get("next")), status_code=303)
    resp.delete_cookie(_flow_cookie(state), path=_FLOW_PATH)
    sign_in.set_cookie(resp, raw, https=redirect_uri.startswith("https://"))
    return resp


@router.post("/auth/logout")
async def logout(request: Request, session: Session = Depends(get_session)) -> JSONResponse:
    """End this browser's Sign-in session (locally) and clear its cookie. A Login token
    is the SPA's to forget."""
    if sign_in.accepts(request) and sign_in.current(request, session) is not None:
        sign_in.end(request, session)
    resp = JSONResponse({"ok": True})
    sign_in.clear_cookie(resp)
    return resp


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
_REASON_PROVIDER_DENIED = "provider_denied"  # the AS said no (access_denied)
_REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"  # the AS is down/overloaded — retry
_REASON_PROVIDER_REJECTED = "provider_rejected_request"  # the AS refused OUR request shape
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


# RFC 6749 §4.1.2.1 error codes, grouped by what the OPERATOR should do about them. The
# provider's code is used only to SELECT one of our literals — never echoed — so the
# redirect keeps its no-remote-input property.
_PROVIDER_UNAVAILABLE_ERRORS = frozenset({"server_error", "temporarily_unavailable"})
# The OIDC codes (OpenID Connect Core §3.1.2.6) belong here rather than with the
# request-shape rejections: they all say the provider wants something from the PERSON —
# consent, a fresh login, an account choice — not that our request was malformed. Reading
# them as "your configuration is wrong" would send the operator to edit a server that is
# fine, when the fix is to click Authenticate and complete the prompt.
_PROVIDER_DENIED_ERRORS = frozenset(
    {
        "access_denied",
        "consent_required",
        "login_required",
        "interaction_required",
        "account_selection_required",
    }
)


def _provider_error_reason(error: str) -> str:
    """Which failure the authorization server actually reported.

    Calling every ``?error=`` a denial tells the operator they were refused when the
    provider was merely down — a wrong diagnosis with a wrong remedy, in the one feature
    whose entire purpose is naming the failure. ``temporarily_unavailable`` is not
    hypothetical: it is what Cloudflare returns when it can't fetch a client-metadata
    document. Anything else (invalid_request, invalid_scope, unauthorized_client, …) is
    the provider rejecting how WE asked, which points at this server's configuration."""
    code = error.strip().lower()
    if code in _PROVIDER_UNAVAILABLE_ERRORS:
        return _REASON_PROVIDER_UNAVAILABLE
    if code in _PROVIDER_DENIED_ERRORS:
        return _REASON_PROVIDER_DENIED
    return _REASON_PROVIDER_REJECTED


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    session: Session = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    iss: str | None = None,
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
        denied_server = oauth_flow.pending_server_id(state) if state else None
        if denied_server is not None:
            logger.warning(
                "OAuth callback returned an error: %s",
                oauth_flow.log_safe(error_description or error),
            )
            # A denial ENDS this sign-in, so retire the flow rather than leaving it
            # parked: otherwise its task sits out the full browser window and then logs
            # a second, misleading "timed out" for a failure already reported here — and
            # the state stays live, so the same callback can be replayed to re-emit
            # operator-level records.
            oauth_flow.cancel_pending(denied_server, superseded=True)
        else:
            # INFO, not WARNING: nothing correlates this to a sign-in we started, so it
            # is indistinguishable from a scanner hitting a public URL. Emitting an
            # operator-level record here would let anyone who can reach the endpoint
            # generate unbounded WARNING volume — and page an operator whose alerting
            # watches that level — for an event no operator needs to act on.
            logger.info(
                "OAuth callback reported an error for an unknown or expired state; "
                "ignoring the unsolicited callback (detail withheld: it is not tied to "
                "any sign-in this instance started)"
            )
        # The provider's code SELECTS one of our literals; it is never echoed, so the
        # redirect stays free of remote input either way. Classifying the uncorrelated
        # case too costs nothing — the answer is a literal — and keeps one code path.
        return _oauth_error(_provider_error_reason(error))
    if not code or not state:
        # A callback carrying a VALID pending state but no code is a broken provider
        # failing a real operator's sign-in — not noise — so it earns the operator-level
        # record and retires the flow, exactly as the denial branch does. Without that,
        # the driver stays parked for the whole browser window and later logs a second,
        # misleading timeout while its state remains replayable.
        stranded = oauth_flow.pending_server_id(state) if state else None
        if stranded is not None:
            logger.warning(
                "OAuth callback for a pending sign-in carried no authorization code — "
                "the provider redirected without one. Start the sign-in again."
            )
            oauth_flow.cancel_pending(stranded, superseded=True)
        else:
            # No correlated state: nobody's sign-in is failing here.
            logger.info("OAuth callback arrived without a code/state pair")
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
        server_id = await oauth_flow.complete_authorization(state, code, iss=iss)
    except KeyError:
        # INFO for the same reason as the branches above — an unmatched state is
        # indistinguishable from noise. The operator whose own flow expired is not left
        # guessing: they get ``reason=expired_or_superseded`` in the toast, which names
        # the cause and the remedy. Every branch BELOW this point is inherently
        # correlated (the state matched a flow this instance started), so those stay at
        # WARNING — a real sign-in really did fail.
        logger.info(
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
    except (oauth_flow.OAuthPromotionBlocked, oauth_flow.OAuthFlowCancelled) as exc:
        # Cancelled flows land here promptly instead of burning the callback budget and
        # reporting a timeout they didn't have. The causes get different codes because
        # they ask different things of the operator: a deleted server has nothing left
        # to inspect OR retry, a superseded or expired attempt just needs starting
        # again, and a configuration change is something to look at before retrying.
        logger.warning("OAuth grant discarded: %s", oauth_flow.log_safe(exc))
        if stopped:
            sup.nudge()
        if getattr(exc, "deleted", False):
            return _oauth_error(_REASON_SERVER_GONE)
        if getattr(exc, "superseded", False):
            return _oauth_error(_REASON_EXPIRED)
        return _oauth_error(_REASON_CONFIG_CHANGED)
    except OAuthTokenError as exc:
        # The authorization server refused the code itself (bad redirect_uri, expired or
        # replayed code, PKCE mismatch, client auth rejected).
        logger.warning("OAuth token exchange failed: %s", oauth_flow.log_safe(exc))
        if stopped:
            sup.nudge()
        return _oauth_error(_REASON_EXCHANGE_FAILED)
    except (TimeoutError, asyncio.TimeoutError, httpx2.TimeoutException) as exc:
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
