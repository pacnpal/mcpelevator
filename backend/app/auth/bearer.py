"""The ``bearer`` auth provider — validates ``Authorization: Bearer <token>``
against the hashed tokens in the database."""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.db import get_engine, repo
from app.db.models import BUILTIN_RUNNER, Server
from app.util import hash_token


class BearerProvider:
    name = "bearer"

    async def authenticate(self, request: Request, server: Server) -> None:
        from app.auth.principal import credential_principal  # circular-import guard
        from app.groups import registry

        group = server.id.removeprefix("group:") if server.id.startswith("group:") else None
        with Session(get_engine()) as session:
            # The built-in mcpelevator server IS the control plane, which authorizes
            # each call as its caller, so it also opens to the credentials the control
            # plane accepts: the break-glass token, any control token, or a Cloudflare
            # Access identity matching a user. A group bundling it opens to an ADMIN's
            # such credential only: groups are an admin-owned surface, and a member's
            # login must not reach the other members' tools.
            if server.runner == BUILTIN_RUNNER or (
                group is not None and registry.includes_builtin(session, group)
            ):
                caller = credential_principal(request, session, cloudflare=True)
                if caller is not None and (group is None or caller.is_admin):
                    return
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            token = token.strip()
            if scheme.lower() != "bearer" or not token:
                raise HTTPException(
                    status_code=401,
                    detail="missing bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            record = repo.get_token_by_hash(session, hash_token(token))
            if record is None:
                raise HTTPException(
                    status_code=401,
                    detail="invalid token",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            scope = record.scope  # read inside the session; the row detaches on exit
        # "all" (the default) authorizes every bearer-protected surface. Otherwise the
        # scope must equal this surface's id: a real server id for a /s/<slug> request,
        # or the synthetic "group:<name>" id for a /g/<name> request (see
        # app.groups.hub.group_server). So a group-A token reaching group B — or any
        # server — is rejected exactly like a wrong-server token, and a per-server token
        # can't authorize a group bundle.
        if scope != "all" and scope != server.id:
            raise HTTPException(
                status_code=403,
                detail="token not authorized for this server",
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
            )
