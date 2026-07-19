"""Bearer-token management endpoints. Plaintext is returned only at creation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import TokenCreate, TokenCreated, TokenInfo
from app.auth import policy
from app.auth import principal as principal_mod
from app.auth.control_plane import enforcement_enabled
from app.auth.principal import Principal, current_principal
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Token
from app.groups import registry as group_registry
from app.registry import service
from app.util import hash_token, new_id, new_token

router = APIRouter()


def _info(session: Session, t: Token) -> TokenInfo:
    user = repo.get_user(session, t.user_id) if t.user_id else None
    return TokenInfo(
        id=t.id, name=t.name, prefix=t.prefix, scope=t.scope,
        user_id=t.user_id, user_name=user.name if user else None,
        created_at=t.created_at,
    )


@router.get("/tokens", response_model=list[TokenInfo])
async def list_tokens(
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    tokens = policy.visible_tokens(principal, repo.list_tokens(session))
    return [_info(session, t) for t in tokens]


@router.post("/tokens", response_model=TokenCreated, status_code=201)
async def create_token(
    payload: TokenCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    # scope is the access boundary: "all" (every bearer-protected server + every group),
    # a specific server id, "group:<name>" (one /g/<name> bundle), or "control" (a
    # control-plane admin token). Reject a blank or dangling value rather than silently
    # widening or minting a token that authorizes nothing.
    scope = payload.scope.strip()
    if not scope:
        raise HTTPException(
            status_code=400,
            detail="scope must be 'all', 'control', 'group:<name>', or a server id",
        )
    raw = new_token()
    token = Token(
        id=new_id(),
        name=payload.name.strip() or "token",
        token_hash=hash_token(raw),
        prefix=raw[:12],
        scope=scope,
        # The minter owns the token (None for synthetic admins) — this is what
        # scopes a member's view of the token table to their own rows.
        user_id=principal.user_id,
    )

    def _persist() -> tuple[int, str] | None:
        """Validate-and-insert as ONE serialized step. EVERY check runs inside the
        config write lock, against a REFRESHED principal, so a mint can't race the
        privilege transitions that commit under this same lock: a demotion (which
        revokes the user's admin-grade tokens) can't miss a privileged token whose
        insert was still in flight, an owner reassignment (which revokes the former
        owner's tokens for the server) can't miss a mint for the just-transferred
        server, and a group token can't be minted for a group being deleted and
        survive its revocation. Returns (status, detail) to reject, None on success."""
        with service.config_write_lock():
            # Drop pre-lock snapshots: the identity map would otherwise satisfy the
            # re-reads below from cached instances (e.g. the principal's user row
            # loaded by resolve()), hiding a demotion committed while we waited.
            session.expire_all()
            fresh = principal_mod.refresh(session, principal)
            if fresh is None:
                return (401, "control-plane auth required")
            # Policy FIRST for the named scopes: a member gets a deterministic 403
            # for "all"/"control"/any group, without learning which groups exist.
            scoped_server = None
            if not scope.startswith("group:") and scope not in ("all", "control"):
                scoped_server = repo.get_server(session, scope)
                if scoped_server is None:
                    return (400, f"unknown server scope {scope!r}")
            # A member mints tokens only for servers they own (never "all",
            # "control", or a group). 400 with the same shape as a dangling id for
            # an invisible server, 403 for the named scopes.
            denial = policy.token_scope_error(fresh, scope, scoped_server)
            if denial is not None:
                return (400 if denial.startswith("unknown server scope") else 403, denial)
            if scope.startswith("group:") and not group_registry.exists(
                session, scope[len("group:"):]
            ):
                return (400, f"unknown group scope {scope!r}")
            repo.create_token(session, token)
            return None

    # Threadpool: the lock is shared with imports deriving scrypt hashes — never
    # wait for it on the event loop.
    rejected = await run_in_threadpool(_persist)
    if rejected is not None:
        raise HTTPException(status_code=rejected[0], detail=rejected[1])
    return TokenCreated(
        id=token.id, name=token.name, prefix=token.prefix,
        scope=token.scope, user_id=token.user_id, user_name=principal.name if token.user_id else None,
        created_at=token.created_at, token=raw,
    )


@router.delete("/tokens/{token_id}", status_code=204)
async def delete_token(
    token_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    # A member deletes only their own tokens; a non-visible id 404s exactly like a
    # nonexistent one (same no-leak semantics as the server routes).
    existing = session.get(Token, token_id)
    if existing is not None and not policy.can_view_token(principal, existing):
        raise HTTPException(status_code=404, detail="token not found")
    # Refuse to remove the last control token if it would leave /api enforced with no
    # credential. The predicate is re-evaluated inside the delete transaction (after the
    # write lock is taken) so a concurrent settings change that just enabled enforcement
    # is seen, closing the delete/enable race. MCPE_ADMIN_TOKEN, if set, lifts the guard.
    def protect(s: Session) -> bool:
        return enforcement_enabled(s) and not get_settings().admin_token

    result = repo.delete_token(session, token_id, protect_last_control=protect)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="token not found")
    if result == "last_control":
        raise HTTPException(
            status_code=409,
            detail="cannot revoke the last admin token while control-plane auth is enforced",
        )
    return Response(status_code=204)
