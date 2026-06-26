"""Bearer-token management endpoints. Plaintext is returned only at creation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import TokenCreate, TokenCreated, TokenInfo
from app.auth.control_plane import enforcement_enabled
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Token
from app.util import hash_token, new_id, new_token

router = APIRouter()


@router.get("/tokens", response_model=list[TokenInfo])
async def list_tokens(session: Session = Depends(get_session)):
    return [
        TokenInfo(id=t.id, name=t.name, prefix=t.prefix, scope=t.scope, created_at=t.created_at)
        for t in repo.list_tokens(session)
    ]


@router.post("/tokens", response_model=TokenCreated, status_code=201)
async def create_token(payload: TokenCreate, session: Session = Depends(get_session)):
    # scope is the access boundary: "all" (every bearer-protected server), a specific
    # server id, or "control" (a control-plane admin token). Reject a blank or dangling
    # value rather than silently widening or minting a token that authorizes nothing.
    scope = payload.scope.strip()
    if not scope:
        raise HTTPException(status_code=400, detail="scope must be 'all', 'control', or a server id")
    if scope not in ("all", "control") and repo.get_server(session, scope) is None:
        raise HTTPException(status_code=400, detail=f"unknown server scope {scope!r}")
    raw = new_token()
    token = Token(
        id=new_id(),
        name=payload.name.strip() or "token",
        token_hash=hash_token(raw),
        prefix=raw[:12],
        scope=scope,
    )
    repo.create_token(session, token)
    return TokenCreated(
        id=token.id, name=token.name, prefix=token.prefix,
        scope=token.scope, created_at=token.created_at, token=raw,
    )


@router.delete("/tokens/{token_id}", status_code=204)
async def delete_token(token_id: str, session: Session = Depends(get_session)):
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
