"""Bearer-token management endpoints. Plaintext is returned only at creation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import TokenCreate, TokenCreated, TokenInfo
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
    # scope is "all" (every server) or a specific server id. It's the access
    # boundary, so reject a blank or dangling value rather than silently widening
    # to "all" (a malformed/uninitialized scope) or minting a token that
    # authorizes nothing. Omitting scope still defaults to "all" via the schema.
    scope = payload.scope.strip()
    if not scope:
        raise HTTPException(status_code=400, detail="scope must be 'all' or a server id")
    if scope != "all" and repo.get_server(session, scope) is None:
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
    if not repo.delete_token(session, token_id):
        raise HTTPException(status_code=404, detail="token not found")
    return Response(status_code=204)
