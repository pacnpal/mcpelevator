"""Bearer-token management endpoints. Plaintext is returned only at creation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import TokenCreate, TokenCreated, TokenInfo
from app.auth import policy
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
    # A server-id scope is validated here (a fast read, no lock). A group scope is
    # validated at INSERT time under the config write lock instead (see _persist below),
    # so it can't race a concurrent group delete.
    scoped_server = None
    if not scope.startswith("group:") and scope not in ("all", "control"):
        scoped_server = repo.get_server(session, scope)
        if scoped_server is None:
            raise HTTPException(status_code=400, detail=f"unknown server scope {scope!r}")
    # Multi-user: a member mints tokens only for servers they own (never "all",
    # "control", or a group). One policy call decides; 400 with the same shape as a
    # dangling id for an invisible server, 403 for the named scopes.
    denial = policy.token_scope_error(principal, scope, scoped_server)
    if denial is not None:
        status = 400 if denial.startswith("unknown server scope") else 403
        raise HTTPException(status_code=status, detail=denial)
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

    def _persist() -> bool:
        """Insert the token. For a group scope, re-check the group exists and insert under
        the config write lock — the SAME lock DELETE /api/groups/{name} holds while it
        revokes the group's tokens and removes it — so a token can't be minted for a group
        being deleted and survive the revocation (which would re-authorize a same-named
        group recreated later). The lock also keeps the wait off the event loop. Returns
        False when the group no longer exists (-> 400)."""
        if scope.startswith("group:"):
            with service.config_write_lock():
                if not group_registry.exists(session, scope[len("group:"):]):
                    return False
                repo.create_token(session, token)
                return True
        repo.create_token(session, token)
        return True

    if not await run_in_threadpool(_persist):
        raise HTTPException(status_code=400, detail=f"unknown group scope {scope!r}")
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
