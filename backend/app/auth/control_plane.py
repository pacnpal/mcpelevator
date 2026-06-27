"""Control-plane (/api) per-request auth — the one place it is decided.

The Host/Origin allowlist (``app.auth.middleware``) still runs first as a second
layer; this adds the bearer-token gate. A control token is required only when
enforcement is on (``control_plane_auth='always'``, or ``'auto'`` while
``bind_mode='expose'``), so a fresh local install stays zero-config. Control-plane
access needs a token with the ``control`` scope; ``MCPE_ADMIN_TOKEN`` is an
always-accepted break-glass credential.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from fastapi import Depends, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Token
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token


def _enforced(mode: str, bind_mode: str, has_public_host: bool, allow_private_lan: bool) -> bool:
    """The enforcement decision, factored out so the request gate and the settings
    lock-out guard agree. ``always`` always enforces; ``auto`` enforces once the
    instance is reachable off-host: ``bind_mode='expose'``, a public base URL is
    configured, OR ``allow_private_lan`` opens it to LAN devices. ``request_allowlist``
    already trusts the public host and the LAN allowance lets private-IP clients in, so
    the token gate must match, or those deployments would expose /api unauthenticated."""
    if mode == "always":
        return True
    return mode == "auto" and (bind_mode == "expose" or has_public_host or allow_private_lan)


def enforcement_enabled(session: Session) -> bool:
    """Is a control token required right now? A loopback-only install with no public
    URL stays zero-config; expose, a configured public URL, or allow_private_lan turns
    the gate on."""
    return _enforced(
        runtime_settings.control_plane_auth(session),
        runtime_settings.bind_mode(session),
        get_settings().public_host is not None,
        runtime_settings.allow_private_lan(session),
    )


def would_lock_out(request: Request, session: Session, changes: dict[str, Any]) -> bool:
    """True if applying ``changes`` would turn enforcement on while THIS request can't
    authenticate as control, locking the operator out the moment it takes effect.
    Enabling enforcement requires a control credential on the request (a control token
    or ``MCPE_ADMIN_TOKEN``); a token row merely existing in the DB is not enough, since
    this browser may not hold its plaintext."""
    if control_auth(request, session) == "ok":
        return False  # the caller already holds a usable control credential
    mode = changes.get("control_plane_auth", runtime_settings.control_plane_auth(session))
    bind = changes.get("bind_mode", runtime_settings.bind_mode(session))
    lan = changes.get("allow_private_lan", runtime_settings.allow_private_lan(session))
    return _enforced(mode, bind, get_settings().public_host is not None, lan)


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def control_auth(request: Request, session: Session) -> Literal["ok", "missing", "wrong_scope"]:
    """Classify the request's control-plane credential. Pure (no raising) so the
    gate and ``/api/auth/status`` share one decision: ``ok`` = a control token (or
    the break-glass env token); ``wrong_scope`` = a valid non-control token;
    ``missing`` = no token or no match."""
    token = _bearer(request)
    if not token:
        return "missing"
    admin = get_settings().admin_token
    if admin and secrets.compare_digest(token, admin):
        return "ok"
    row = repo.get_token_by_hash(session, hash_token(token))
    if row is None:
        return "missing"
    return "ok" if row.scope == "control" else "wrong_scope"


def require_control_plane(request: Request, session: Session = Depends(get_session)) -> None:
    """FastAPI dependency gating the control-plane routers. A no-op when enforcement
    is off, which is what keeps the local zero-config experience working."""
    if not enforcement_enabled(session):
        return
    result = control_auth(request, session)
    if result == "ok":
        return
    if result == "wrong_scope":
        raise HTTPException(status_code=403, detail="control scope required")
    raise HTTPException(
        status_code=401,
        detail="control-plane auth required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def ensure_control_token(session: Session) -> str | None:
    """Mint a control token if none exists (idempotent). Returns the plaintext once,
    or ``None`` when one already exists or ``MCPE_ADMIN_TOKEN`` supplies the
    credential. Called from startup and from the UI's generate-admin action."""
    if get_settings().admin_token or repo.control_token_exists(session):
        return None
    raw = new_token()
    repo.create_token(
        session,
        Token(id=new_id(), name="admin", token_hash=hash_token(raw), prefix=raw[:12], scope="control"),
    )
    return raw
