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

# Sentinel distinguishing "not yet resolved this request" from a cached None.
_UNRESOLVED = object()


def _effective_local_runners(user) -> bool:
    """EFFECTIVE permission, not the stored flag: policy permits every runner for
    admins regardless of the flag, and the SPA renders from this field — the two
    must agree, or an admin whose row happens to hold false gets a crippled form.
    The ONE calculation both resolution paths use (same discipline as
    ``is_env_admin_token``), so they can never disagree."""
    return bool(user.local_runners) or user.role == ROLE_ADMIN


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
    and the gating dependency share one decision.

    Memoized on ``request.state`` (identity can't change mid-request — the header
    is fixed), so a gated handler that both passes the gate and asks for the
    principal costs one token/user lookup, not two."""
    cached = getattr(request.state, "mcpe_principal", _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached
    principal = _resolve_uncached(request, session)
    request.state.mcpe_principal = principal
    return principal


def _resolve_uncached(request: Request, session: Session) -> Optional[Principal]:
    from app.auth.control_plane import (  # circular-import guard
        enforcement_enabled,
        is_env_admin_token,
    )

    # Enforcement OFF decides FIRST: the local operator is trusted in full
    # regardless of any bearer header — exactly the pre-multi-user behavior, where
    # /api never read the header at all. This must also outrank a stored MEMBER
    # login token: after an admin turns enforcement off, a browser still sending
    # its member credential must get the documented zero-config local-admin
    # surface, not a member view it can only escape by clearing the token.
    if not enforcement_enabled(session):
        return LOCAL_ADMIN

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
                    local_runners=_effective_local_runners(user),
                )
            # else: dangling credential — fail closed (enforcement is on here).
    return None


def refresh(session: Session, principal: Principal) -> Optional[Principal]:
    """Re-read a user-bound principal's role/flags from the DB — the check-time
    truth for authorization decisions made INSIDE a serialized write. A request
    resolves its principal once at entry; an admin may demote the user or revoke
    their local-runner permission (both committed under the config write lock)
    while the request is queued, so every policy check performed under that lock
    must use this refreshed view, not the entry-time snapshot. Synthetic
    principals (user_id None) are immutable and returned unchanged. Returns None
    when the user row is gone — the caller should fail closed (401)."""
    if principal.user_id is None:
        return principal
    user = repo.get_user(session, principal.user_id)
    if user is None:
        return None
    return Principal(
        user_id=user.id, name=user.name, role=user.role,
        local_runners=_effective_local_runners(user),
    )


def admin_now(session: Session, principal: Principal) -> bool:
    """Is the CALLER still an admin, judged on committed state? For re-authorizing
    INSIDE a serialized write (or after any await): the router-level
    ``require_admin`` runs at request entry, and a demotion or deletion of the
    caller can commit while the request waits. ``expire_all`` first so the request
    session's identity map can't satisfy the re-read with the entry-time row."""
    session.expire_all()
    fresh = refresh(session, principal)
    return fresh is not None and fresh.is_admin


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
