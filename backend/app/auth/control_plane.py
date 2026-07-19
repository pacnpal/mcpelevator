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

from app.auth.principal import ROLE_ADMIN
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Token
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token


def _has_off_host_origin() -> bool:
    """True when the operator has declared an off-host origin via env — a public base URL
    (``MCPE_PUBLIC_BASE_URL``) or extra allowed hosts (``MCPE_ALLOWED_HOSTS``). Both are
    always trusted by ``request_allowlist``, so a request reaching /api via that hostname
    must require a token under ``auto`` or those routes would be exposed unauthenticated."""
    settings = get_settings()
    return settings.public_host is not None or bool(settings.extra_allowed_hosts)


def _enforced(mode: str, bind_mode: str, has_off_host_origin: bool, allow_private_lan: bool) -> bool:
    """The enforcement decision, factored out so the request gate and the settings
    lock-out guard agree. ``always`` always enforces; ``auto`` enforces once the
    instance is reachable off-host: ``bind_mode='expose'``, an off-host origin is declared
    (a public base URL or extra allowed hosts), OR ``allow_private_lan`` opens it to LAN
    devices. ``request_allowlist`` already trusts those hosts and the LAN allowance lets
    private-IP clients in, so the token gate must match, or those deployments would expose
    /api unauthenticated."""
    if mode == "always":
        return True
    return mode == "auto" and (bind_mode == "expose" or has_off_host_origin or allow_private_lan)


def enforcement_enabled(session: Session) -> bool:
    """Is a control token required right now? A loopback-only install with no declared
    off-host origin stays zero-config; expose, a configured public URL / extra allowed
    hosts, or allow_private_lan turns the gate on."""
    return _enforced(
        runtime_settings.control_plane_auth(session),
        runtime_settings.bind_mode(session),
        _has_off_host_origin(),
        runtime_settings.allow_private_lan(session),
    )


def admin_credential_presented(request: Request, session: Session) -> bool:
    """Does the request carry a credential that resolves to ADMIN — the env token,
    a user-less control token, or an admin user's login? Unlike principal
    resolution this ignores enforcement state entirely: it classifies the
    presented token itself, for decisions about turning enforcement ON. A member's
    login token is a valid control credential but must NOT satisfy this — enabling
    enforcement while holding only member logins would strand the box with no way
    to reach Settings or Users."""
    token = _bearer(request)
    if is_env_admin_token(token):
        return True
    if not token:
        return False
    row = repo.get_token_by_hash(session, hash_token(token))
    if row is None or row.scope != "control":
        return False
    if row.user_id is None:
        return True  # legacy/boot mint: resolves to admin
    user = repo.get_user(session, row.user_id)
    return user is not None and user.role == ROLE_ADMIN


def would_lock_out(request: Request, session: Session, changes: dict[str, Any]) -> bool:
    """True if applying ``changes`` would turn enforcement on while THIS request
    can't authenticate as an ADMIN, locking the operator out of the admin surfaces
    the moment it takes effect. Enabling enforcement requires an admin-resolving
    credential on the request (``admin_credential_presented``) — a token row merely
    existing in the DB is not enough (this browser may not hold its plaintext),
    and a member login is not enough (Settings/Users would become unreachable)."""
    if admin_credential_presented(request, session):
        return False  # the caller already holds a usable admin credential
    mode = changes.get("control_plane_auth", runtime_settings.control_plane_auth(session))
    bind = changes.get("bind_mode", runtime_settings.bind_mode(session))
    lan = changes.get("allow_private_lan", runtime_settings.allow_private_lan(session))
    return _enforced(mode, bind, _has_off_host_origin(), lan)


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def is_env_admin_token(token: str) -> bool:
    """Does ``token`` match the ``MCPE_ADMIN_TOKEN`` break-glass credential? The ONE
    comparison both the gate (``control_auth``) and identity resolution
    (``principal.resolve``) use, so they can never disagree on break-glass access."""
    admin = get_settings().admin_token
    return bool(token and admin and secrets.compare_digest(token, admin))


def control_auth(request: Request, session: Session) -> Literal["ok", "missing", "wrong_scope"]:
    """Classify the request's control-plane credential. Pure (no raising) so the
    gate and ``/api/auth/status`` share one decision: ``ok`` = a control token (or
    the break-glass env token); ``wrong_scope`` = a valid non-control token;
    ``missing`` = no token or no match."""
    token = _bearer(request)
    if not token:
        return "missing"
    if is_env_admin_token(token):
        return "ok"
    row = repo.get_token_by_hash(session, hash_token(token))
    if row is None:
        return "missing"
    if row.scope != "control":
        return "wrong_scope"
    # A user-bound credential whose user row is gone fails closed, matching
    # principal.resolve — the gate and identity resolution must agree.
    if row.user_id is not None and repo.get_user(session, row.user_id) is None:
        return "missing"
    return "ok"


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


def mint_control_token(session: Session) -> str:
    """Mint a new control token unconditionally and return its plaintext once. Existing
    tokens keep working — this only adds one. Use ``ensure_control_token`` for the
    idempotent startup path; this is the force/recovery path."""
    raw = new_token()
    repo.create_token(
        session,
        Token(id=new_id(), name="admin", token_hash=hash_token(raw), prefix=raw[:12], scope="control"),
    )
    return raw


def ensure_control_token(session: Session) -> str | None:
    """Mint an ADMIN control token if no admin-resolving credential exists
    (idempotent). Returns the plaintext once, or ``None`` when an admin login
    already exists or ``MCPE_ADMIN_TOKEN`` supplies the credential. Called from
    startup and from the UI's generate-admin action. Checks for an ADMIN
    credential, not merely any control-scoped row: a member login token is a
    valid control credential but can't reach Settings/Users — a box whose only
    login is a member's must still get an admin token minted when enforcement
    turns on (e.g. via a newly added off-host env setting + restart)."""
    if get_settings().admin_token or repo.admin_credential_exists(session):
        return None
    return mint_control_token(session)
