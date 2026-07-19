"""WHO is calling the control plane — the one place identity is resolved.

Every /api handler that cares about identity depends on ``current_principal``;
nothing else inspects the Authorization header for identity. Resolution is
deterministic and upgrade-safe:

- enforcement OFF            -> the synthetic local admin (zero-config unchanged)
- ``MCPE_ADMIN_TOKEN`` match -> the synthetic env admin (break-glass, always admin)
- control token, user_id set -> that user's role/flags (missing user row fails
                                closed: the credential is treated as invalid)
- control token, no user_id  -> admin (tokens minted before multi-user, and
                                boot/recovery mints, keep full power on upgrade)

WHAT a principal may do lives in ``app.auth.policy`` — keep the two concerns
separate so authorization rules never re-derive identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.db import get_session, repo
from app.util import hash_token

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


@dataclass(frozen=True)
class Principal:
    """The resolved control-plane caller. ``user_id`` is None for the synthetic
    admins (enforcement off, the env break-glass token, or a legacy user-less
    control token) — they own nothing and see everything."""

    user_id: Optional[str]
    name: str
    role: str
    local_runners: bool

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


# The synthetic admins are constants, not constructed per request, so identity
# comparisons and log lines stay deterministic.
LOCAL_ADMIN = Principal(user_id=None, name="local operator", role=ROLE_ADMIN, local_runners=True)
ENV_ADMIN = Principal(user_id=None, name="MCPE_ADMIN_TOKEN", role=ROLE_ADMIN, local_runners=True)
LEGACY_ADMIN = Principal(user_id=None, name="admin", role=ROLE_ADMIN, local_runners=True)


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def resolve(request: Request, session: Session) -> Optional[Principal]:
    """The request's principal, or None when it carries no usable control
    credential. Pure classification — never raises — so the auth-status endpoint
    and the gating dependency share one decision."""
    from app.auth.control_plane import (  # circular-import guard
        enforcement_enabled,
        is_env_admin_token,
    )

    token = _bearer(request)
    if is_env_admin_token(token):
        return ENV_ADMIN
    if token:
        row = repo.get_token_by_hash(session, hash_token(token))
        if row is not None and row.scope == "control":
            if row.user_id is None:
                return LEGACY_ADMIN
            user = repo.get_user(session, row.user_id)
            if user is not None:
                return Principal(
                    user_id=user.id, name=user.name, role=user.role,
                    local_runners=bool(user.local_runners),
                )
            # else: dangling credential — fall through to the enforcement check
            # (fails closed when enforcement is on).
    # No usable credential on the request. With enforcement OFF the local operator
    # is trusted in full REGARDLESS of any stray/data-plane bearer header — exactly
    # the pre-multi-user behavior, where /api never read the header at all.
    if not enforcement_enabled(session):
        return LOCAL_ADMIN
    return None


def current_principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    """FastAPI dependency: the caller's principal. Raises the same 401 shape as
    ``require_control_plane`` when none resolves — handlers behind the gate should
    never see that in practice, but failing closed here means a handler can rely
    on always holding a Principal."""
    principal = resolve(request, session)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="control-plane auth required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_admin(request: Request, session: Session = Depends(get_session)) -> Principal:
    """FastAPI dependency for admin-only routers/handlers (settings writes, groups,
    user management). 403 (not 404): these routes aren't per-resource, so there is
    no existence to hide — a member should learn they lack the role."""
    principal = current_principal(request, session)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal
