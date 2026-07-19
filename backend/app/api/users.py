"""User management — the multi-user control plane's admin surface.

Admin-only (router-level ``require_admin``). Users hold no passwords: an admin
creates a user, mints them a login credential (a ``control``-scoped token bound
via ``Token.user_id``), and hands over the plaintext — shown exactly once, like
every token. Deleting a user is deterministic, never cascading: it is refused
while the user still owns servers (reassign or delete them first) and revokes
every token bound to the user in the same transaction.

The one invariant guarded here: at least one usable ADMIN login must survive any
demotion or deletion (``repo.admin_credential_exists``), unless MCPE_ADMIN_TOKEN
provides break-glass access — the same lock-out philosophy as the last-control-
token guard on token deletes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import UserCreate, UserCredential, UserInfo, UserUpdate
from app.auth.principal import require_admin
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Token, User
from app.util import hash_token, new_id, new_token

router = APIRouter(dependencies=[Depends(require_admin)])


def _info(session: Session, user: User) -> UserInfo:
    tokens = [t for t in repo.list_tokens(session) if t.user_id == user.id]
    return UserInfo(
        id=user.id,
        name=user.name,
        role=user.role,
        local_runners=bool(user.local_runners),
        servers_count=repo.count_servers_owned(session, user.id),
        tokens_count=len(tokens),
        created_at=user.created_at,
    )


def _admin_login_would_remain(session: Session, *, excluding_user_id: str) -> bool:
    """Would an admin still be able to log in if this user lost admin power?
    MCPE_ADMIN_TOKEN always works, so it lifts the guard."""
    if get_settings().admin_token:
        return True
    return repo.admin_credential_exists(session, excluding_user_id=excluding_user_id)


@router.get("/users", response_model=list[UserInfo])
async def list_users(session: Session = Depends(get_session)):
    return [_info(session, u) for u in repo.list_users(session)]


@router.post("/users", response_model=UserInfo, status_code=201)
async def create_user(payload: UserCreate, session: Session = Depends(get_session)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")
    user = User(id=new_id(), name=name, role=payload.role, local_runners=payload.local_runners)
    return _info(session, repo.create_user(session, user))


@router.patch("/users/{user_id}", response_model=UserInfo)
async def update_user(
    user_id: str, payload: UserUpdate, session: Session = Depends(get_session)
):
    user = repo.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name must not be empty")
        user.name = name
    if payload.role is not None and payload.role != user.role:
        if user.role == "admin" and not _admin_login_would_remain(
            session, excluding_user_id=user.id
        ):
            raise HTTPException(
                status_code=409,
                detail="cannot demote the last admin — no other admin login would remain",
            )
        user.role = payload.role
    if payload.local_runners is not None:
        user.local_runners = payload.local_runners
    return _info(session, repo.save_user(session, user))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, session: Session = Depends(get_session)):
    user = repo.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    owned = repo.count_servers_owned(session, user_id)
    if owned:
        raise HTTPException(
            status_code=409,
            detail=(
                f"user still owns {owned} server(s) — delete them or reassign their "
                "owner before deleting the user"
            ),
        )
    if user.role == "admin" and not _admin_login_would_remain(session, excluding_user_id=user.id):
        raise HTTPException(
            status_code=409,
            detail="cannot delete the last admin — no other admin login would remain",
        )
    repo.delete_user_and_tokens(session, user_id)
    return Response(status_code=204)


@router.post("/users/{user_id}/credentials", response_model=UserCredential, status_code=201)
async def mint_credential(user_id: str, session: Session = Depends(get_session)):
    """Mint a login (control) token for this user; plaintext returned exactly once.
    Minting is additive — existing credentials keep working (rotate by deleting
    the old token from the tokens table)."""
    user = repo.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    raw = new_token()
    token = Token(
        id=new_id(),
        name=f"login: {user.name}",
        token_hash=hash_token(raw),
        prefix=raw[:12],
        scope="control",
        user_id=user.id,
    )
    repo.create_token(session, token)
    return UserCredential(token_id=token.id, token=raw, prefix=token.prefix)
