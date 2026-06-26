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
    raw = new_token()
    token = Token(
        id=new_id(),
        name=payload.name.strip() or "token",
        token_hash=hash_token(raw),
        prefix=raw[:12],
        scope=payload.scope,
    )
    repo.create_token(session, token)
    return TokenCreated(
        id=token.id, name=token.name, prefix=token.prefix,
        scope=token.scope, created_at=token.created_at, token=raw,
    )


@router.delete("/tokens/{token_id}", status_code=204)
async def delete_token(token_id: str, session: Session = Depends(get_session)):
    # When enforcement is on (and no MCPE_ADMIN_TOKEN break-glass), refuse to remove
    # the last control token: it would gate /api, including minting a replacement, and
    # lock the operator out. The check and delete are atomic in the repo so concurrent
    # deletes can't both slip through.
    keep_last_control = enforcement_enabled(session) and not get_settings().admin_token
    result = repo.delete_token(session, token_id, keep_last_control=keep_last_control)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="token not found")
    if result == "last_control":
        raise HTTPException(
            status_code=409,
            detail="cannot revoke the last admin token while control-plane auth is enforced",
        )
    return Response(status_code=204)
