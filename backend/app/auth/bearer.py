"""The ``bearer`` auth provider — validates ``Authorization: Bearer <token>``
against the hashed tokens in the database."""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.db import get_engine, repo
from app.db.models import Server
from app.util import hash_token


class BearerProvider:
    name = "bearer"

    async def authenticate(self, request: Request, server: Server) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=401,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        with Session(get_engine()) as session:
            record = repo.get_token_by_hash(session, hash_token(token))
            if record is None:
                raise HTTPException(
                    status_code=401,
                    detail="invalid token",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            scope = record.scope  # read inside the session; the row detaches on exit
        # A token scoped to a specific server authorizes only that server; "all"
        # (the default) authorizes every bearer-protected server.
        if scope != "all" and scope != server.id:
            raise HTTPException(
                status_code=403,
                detail="token not authorized for this server",
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
            )
