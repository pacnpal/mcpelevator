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

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import UserCreate, UserCredential, UserInfo, UserUpdate
from app.auth import principal as principal_mod
from app.auth.principal import Principal, require_admin
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Token, User
from app.registry import service
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token

router = APIRouter(dependencies=[Depends(require_admin)])


def _info(
    session: Session,
    user: User,
    *,
    servers_count: int | None = None,
    tokens_count: int | None = None,
    managed: dict[str, bool] | None = None,
) -> UserInfo:
    """One user's API shape. The counts default to per-user queries (fine for the
    single-user endpoints); ``list_users`` passes bulk-computed values instead so
    listing N users costs a constant number of queries, not 2N+1."""
    if managed is None:
        managed = runtime_settings.managed_user_fields(session)
    if tokens_count is None:
        tokens_count = sum(1 for t in repo.list_tokens(session) if t.user_id == user.id)
    if servers_count is None:
        servers_count = repo.count_servers_owned(session, user.id)
    return UserInfo(
        id=user.id,
        name=user.name,
        role=user.role,
        local_runners=bool(user.local_runners),
        control_mcp=bool(user.control_mcp),
        servers_count=servers_count,
        tokens_count=tokens_count,
        created_at=user.created_at,
        linked_issuer=user.oidc_issuer,
        active=bool(user.active),
        managed=managed,
    )


def _overprivileged_token_ids(session: Session, user_id: str) -> list[str]:
    """The user's data-plane tokens a MEMBER could not have minted: "all", any
    group scope, and server scopes for servers they don't own. Control tokens are
    excluded — they are the login itself and follow the user's current role."""
    ids: list[str] = []
    for token in repo.list_tokens(session):
        if token.user_id != user_id or token.scope == "control":
            continue
        if token.scope == "all" or token.scope.startswith("group:"):
            ids.append(token.id)
            continue
        server = repo.get_server(session, token.scope)
        if server is None or server.owner_id != user_id:
            ids.append(token.id)
    return ids


# Caller re-authorization under the lock (see principal.admin_now): a demotion
# can commit while a request waits, and a just-demoted admin must not keep
# mutating users — e.g. re-promoting themselves via a queued PATCH.
_admin_now = principal_mod.admin_now


_FORBIDDEN = HTTPException(status_code=403, detail="admin role required")


def _admin_login_would_remain(session: Session, *, excluding_user_id: str) -> bool:
    """Would an admin still be able to log in if this user lost admin power?
    MCPE_ADMIN_TOKEN always works, so it lifts the guard."""
    if get_settings().admin_token:
        return True
    return repo.admin_credential_exists(session, excluding_user_id=excluding_user_id)


def change_user(
    session: Session,
    user: User,
    *,
    name: str | None = None,
    role: str | None = None,
    local_runners: bool | None = None,
    control_mcp: bool | None = None,
    active: bool | None = None,
    commit: bool = True,
) -> User | str:
    """The one user mutation, shared by ``PATCH /users`` and the SSO group sync. The
    caller holds the config write lock. Fields left None stay. Returns the saved user,
    or "last_admin" (nothing written) when a demotion or deactivation would leave no
    admin login. ``commit=False`` only flushes, so the caller commits it with its
    other writes.

    Deactivating ends the user's Sign-in sessions and revokes every token they hold,
    in the same transaction as the flag. Their servers stay theirs. Reactivating only
    sets the flag: revoked tokens stay revoked."""
    if active is False and user.active and user.role == "admin":
        if not _admin_login_would_remain(session, excluding_user_id=user.id):
            return "last_admin"
    if role is not None and role != user.role:
        if user.role == "admin" and not _admin_login_would_remain(
            session, excluding_user_id=user.id
        ):
            return "last_admin"
        if user.role == "admin" and role == "member":
            # Demotion revokes the data-plane tokens the user could only mint AS an
            # admin ("all", group scopes, and scopes for servers they don't own),
            # otherwise those tokens would keep authorizing endpoints the member role
            # can't reach. Revoke BEFORE the role save so an interruption leaves the
            # benign state (an admin with fewer tokens), never a member holding
            # admin-grade credentials. Login (control) tokens stay: they simply
            # resolve to the member role from now on.
            repo.delete_tokens_by_ids(
                session, _overprivileged_token_ids(session, user.id), commit=commit
            )
        user.role = role
    if name is not None:
        user.name = name.strip()
    if local_runners is not None:
        user.local_runners = local_runners
    if control_mcp is not None:
        user.control_mcp = control_mcp
    if active is False and user.active:
        repo.revoke_user_credentials(session, user.id)
    if active is not None:
        user.active = active
    return repo.save_user(session, user, commit=commit)


@router.get("/users", response_model=list[UserInfo])
async def list_users(session: Session = Depends(get_session)):
    servers_by_user = Counter(
        s.owner_id for s in repo.list_servers(session) if s.owner_id is not None
    )
    tokens_by_user = Counter(
        t.user_id for t in repo.list_tokens(session) if t.user_id is not None
    )
    managed = runtime_settings.managed_user_fields(session)
    return [
        _info(
            session,
            u,
            servers_count=servers_by_user.get(u.id, 0),
            tokens_count=tokens_by_user.get(u.id, 0),
            managed=managed,
        )
        for u in repo.list_users(session)
    ]


@router.post("/users", response_model=UserInfo, status_code=201)
async def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")
    user = User(
        id=new_id(),
        name=name,
        role=payload.role,
        local_runners=payload.local_runners,
        control_mcp=payload.control_mcp,
    )

    def _create():
        with service.config_write_lock():
            session.expire_all()
            if not _admin_now(session, principal):
                return None
            return repo.create_user(session, user)

    created = await run_in_threadpool(_create)
    if created is None:
        raise _FORBIDDEN
    return _info(session, created)


@router.patch("/users/{user_id}", response_model=UserInfo)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    if payload.name is not None and not payload.name.strip():
        raise HTTPException(status_code=400, detail="name must not be empty")

    def _apply():
        """The whole mutation — caller re-authorization, re-fetch, last-admin
        guard, demotion revocation, field writes — under ONE hold of the config
        write lock, so two concurrent demotions/deletions can't both pass the
        guard on each other's still-present credential, and a just-demoted admin's
        queued PATCH can't re-promote them."""
        with service.config_write_lock():
            session.expire_all()  # re-reads must see state committed while we waited
            if not _admin_now(session, principal):
                return "forbidden"
            user = repo.get_user(session, user_id)
            if user is None:
                return "not_found"
            # A field provider groups manage would be reset at the next sign-in, so a
            # hand edit is refused rather than silently reverted.
            managed = runtime_settings.managed_user_fields(session)
            for field in ("role", "local_runners"):
                if getattr(payload, field) is not None and managed[field]:
                    return ("managed", runtime_settings.MANAGED_USER_FIELDS[field])
            return change_user(
                session, user, name=payload.name, role=payload.role,
                local_runners=payload.local_runners, control_mcp=payload.control_mcp,
                active=payload.active,
            )

    # Threadpool: the lock is a threading lock shared with imports deriving scrypt
    # hashes — never wait for it on the event loop.
    result = await run_in_threadpool(_apply)
    if result == "forbidden":
        raise _FORBIDDEN
    if result == "not_found":
        raise HTTPException(status_code=404, detail="user not found")
    if isinstance(result, tuple):
        raise HTTPException(
            status_code=409,
            detail=f"managed by the {result[1]} setting: the identity provider's groups set it at sign-in",
        )
    if result == "last_admin":
        verb = "deactivate" if payload.active is False else "demote"
        raise HTTPException(
            status_code=409,
            detail=f"cannot {verb} the last admin — no other admin login would remain",
        )
    return _info(session, result)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    def _delete():
        """EVERY check runs under the same hold of the config write lock as the
        delete itself: the last-admin guard (two concurrent deletes of the two
        remaining admins must not both pass by seeing each other's still-present
        credential — the lock serializes them, so the second re-reads a world where
        the first admin is gone and is refused) and the owns-servers check (the
        same lock owner reassignment holds while writing owner_id, so a
        reassignment can't land between check and delete and leave a server
        pointing at a deleted user). Returns 'not_found' | 'last_admin' | an owned
        count | 'ok'."""
        with service.config_write_lock():
            session.expire_all()  # re-reads must see state committed while we waited
            if not _admin_now(session, principal):
                return "forbidden"
            user = repo.get_user(session, user_id)
            if user is None:
                return "not_found"
            if user.role == "admin" and not _admin_login_would_remain(
                session, excluding_user_id=user.id
            ):
                return "last_admin"
            owned = repo.count_servers_owned(session, user_id)
            if owned:
                return owned
            repo.delete_user_and_tokens(session, user_id)
            return "ok"

    # Threadpool: the lock is a threading lock shared with imports deriving scrypt
    # hashes — never wait for it on the event loop.
    result = await run_in_threadpool(_delete)
    if result == "forbidden":
        raise _FORBIDDEN
    if result == "not_found":
        raise HTTPException(status_code=404, detail="user not found")
    if result == "last_admin":
        raise HTTPException(
            status_code=409,
            detail="cannot delete the last admin — no other admin login would remain",
        )
    if result != "ok":
        raise HTTPException(
            status_code=409,
            detail=(
                f"user still owns {result} server(s) — delete them or reassign their "
                "owner before deleting the user"
            ),
        )
    return Response(status_code=204)


@router.delete("/users/{user_id}/identity", status_code=204)
async def unlink_identity(
    user_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    """Clear a user's Linked identity and end their Sign-in sessions. Their next SSO
    sign-in matches by name again, which is how users move to a new issuer. Under the
    config write lock, which the sign-in's link write holds too."""

    def _unlink():
        with service.config_write_lock():
            session.expire_all()
            if not _admin_now(session, principal):
                return "forbidden"
            return "ok" if repo.unlink_user_identity(session, user_id) else "not_found"

    result = await run_in_threadpool(_unlink)
    if result == "forbidden":
        raise _FORBIDDEN
    if result == "not_found":
        raise HTTPException(status_code=404, detail="user not found")
    return Response(status_code=204)


@router.post("/users/{user_id}/credentials", response_model=UserCredential, status_code=201)
async def mint_credential(
    user_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    """Mint a login (control) token for this user; plaintext returned exactly once.
    Minting is additive — existing credentials keep working (rotate by deleting
    the old token from the tokens table). Validate-and-insert runs under the
    config write lock like every credential mutation: the caller's admin role is
    re-checked there (a demoted admin must not mint logins), and the insert can't
    race the guards that count control tokens."""
    raw = new_token()

    def _mint():
        with service.config_write_lock():
            session.expire_all()
            if not _admin_now(session, principal):
                return "forbidden"
            user = repo.get_user(session, user_id)
            if user is None:
                return "not_found"
            if not user.active:
                # A login waiting on a Deactivated user would work the moment they're
                # reactivated, and revoked tokens must stay revoked.
                return "inactive"
            token = Token(
                id=new_id(),
                name=f"login: {user.name}",
                token_hash=hash_token(raw),
                prefix=raw[:12],
                scope="control",
                user_id=user.id,
            )
            repo.create_token(session, token)
            return token

    result = await run_in_threadpool(_mint)
    if result == "forbidden":
        raise _FORBIDDEN
    if result == "not_found":
        raise HTTPException(status_code=404, detail="user not found")
    if result == "inactive":
        raise HTTPException(
            status_code=409, detail="user is deactivated: reactivate the user first"
        )
    return UserCredential(token_id=result.id, token=raw, prefix=result.prefix)
