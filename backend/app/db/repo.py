"""Typed CRUD — the ONLY module that writes SSOT rows (INSERT/UPDATE/DELETE).

Keeping writes funneled through here enforces SSOT at the code level: services
and the reconciler read freely, but mutate only via these functions.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Optional

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.models import Server, ServerRuntime, Setting, Token, User, utcnow

# --------------------------------------------------------------------------- #
# servers (desired state)
# --------------------------------------------------------------------------- #


def create_server(session: Session, server: Server) -> Server:
    session.add(server)
    session.commit()
    session.refresh(server)
    return server


def get_server(session: Session, server_id: str) -> Optional[Server]:
    return session.get(Server, server_id)


def get_server_by_slug(session: Session, slug: str) -> Optional[Server]:
    return session.exec(select(Server).where(Server.slug == slug)).first()


def list_servers(session: Session) -> list[Server]:
    return list(session.exec(select(Server).order_by(Server.created_at)).all())


def save_server(session: Session, server: Server) -> Server:
    server.updated_at = utcnow()
    session.add(server)
    session.commit()
    session.refresh(server)
    return server


def set_config_hash(session: Session, server_id: str, config_hash: str) -> None:
    """Update only the stored config_hash (no updated_at bump) — used by the boot
    backfill so an upgraded row's hash matches the current input shape without
    looking like a user edit."""
    server = session.get(Server, server_id)
    if server is not None:
        server.config_hash = config_hash
        session.add(server)
        session.commit()


def set_auth_provider(session: Session, server_id: str, auth_provider: str) -> None:
    """Update only the stored auth_provider (no updated_at bump) — used by the boot
    normalization of legacy free-text values into the canonical set."""
    server = session.get(Server, server_id)
    if server is not None:
        server.auth_provider = auth_provider
        session.add(server)
        session.commit()


def set_owner(session: Session, server_id: str, owner_id: Optional[str]) -> None:
    """Update only the stored owner_id (no updated_at bump): ownership is identity,
    not launch config — reassigning must neither look like a config edit nor feed
    the startup-status clock."""
    server = session.get(Server, server_id)
    if server is not None:
        server.owner_id = owner_id
        session.add(server)
        session.commit()


def delete_server(session: Session, server_id: str) -> bool:
    server = session.get(Server, server_id)
    if server is None:
        return False
    runtime = session.get(ServerRuntime, server_id)
    if runtime is not None:
        session.delete(runtime)
    session.delete(server)
    session.commit()
    return True


# --------------------------------------------------------------------------- #
# runtime (observed state — reconciler-owned)
# --------------------------------------------------------------------------- #


def get_runtime(session: Session, server_id: str) -> Optional[ServerRuntime]:
    return session.get(ServerRuntime, server_id)


def upsert_runtime(session: Session, server_id: str, **fields: Any) -> ServerRuntime:
    runtime = session.get(ServerRuntime, server_id)
    if runtime is None:
        runtime = ServerRuntime(server_id=server_id, tools=fields.pop("tools", []))
    for key, value in fields.items():
        setattr(runtime, key, value)
    runtime.updated_at = utcnow()
    session.add(runtime)
    session.commit()
    session.refresh(runtime)
    return runtime


def reset_all_runtime(session: Session) -> None:
    """Bulk-reset all observed runtime to stopped in one statement (used on boot,
    where runtime from a prior process is stale). Servers with no runtime row are
    already 'stopped' to the API, so updating existing rows is sufficient."""
    session.execute(
        update(ServerRuntime).values(
            state="stopped",
            pid=None,
            port=None,
            last_error=None,
            restart_count=0,
            last_health=None,
            tools=[],
            updated_at=utcnow(),
        )
    )
    session.commit()


# --------------------------------------------------------------------------- #
# settings (runtime-mutable key/value; JSON-encoded values)
# --------------------------------------------------------------------------- #


def setting_get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    if row is None:
        return default
    return json.loads(row.value)


def setting_set_many(
    session: Session,
    items: dict[str, Any],
    *,
    guard: Callable[[Session], None] | None = None,
) -> None:
    """Set several settings atomically (one commit), so a multi-field patch never
    partially applies. If ``guard`` is given it runs after the rows are staged and the
    write lock is taken (``flush``), before commit, and may raise to abort (the
    transaction is rolled back). Holding the lock means a concurrent writer, such as a
    token delete, can't change the guard's view between the check and the commit."""
    for key, value in items.items():
        row = session.get(Setting, key)
        encoded = json.dumps(value)
        if row is None:
            row = Setting(key=key, value=encoded)
        else:
            row.value = encoded
        session.add(row)
    if guard is not None:
        session.flush()  # take the write lock before the guard re-reads state
        try:
            guard(session)
        except Exception:
            session.rollback()
            raise
    session.commit()


# --------------------------------------------------------------------------- #
# tokens (bearer auth; hash-only storage)
# --------------------------------------------------------------------------- #


def create_token(session: Session, token: Token) -> Token:
    session.add(token)
    session.commit()
    session.refresh(token)
    return token


def list_tokens(session: Session) -> list[Token]:
    return list(session.exec(select(Token).order_by(Token.created_at)).all())


def get_token_by_hash(session: Session, token_hash: str) -> Optional[Token]:
    return session.exec(select(Token).where(Token.token_hash == token_hash)).first()


def delete_tokens_by_scope(session: Session, scope: str) -> int:
    """Hard-delete every token carrying exactly ``scope``; returns the count removed.

    Used when a group is deleted: a ``group:<name>`` scope is a deterministic string
    (unlike a random server id), so leaving its tokens behind would let them silently
    re-authorize a *different* group later recreated under the same name. Revoking them
    on delete keeps "delete the group" meaning "revoke access to it"."""
    tokens = list(session.exec(select(Token).where(Token.scope == scope)).all())
    for token in tokens:
        session.delete(token)
    if tokens:
        session.commit()
    return len(tokens)


def delete_tokens_by_ids(session: Session, token_ids: list[str]) -> int:
    """Hard-delete the given tokens in one transaction; returns the count removed.
    Used for policy-driven revocations (owner reassignment, admin demotion) where
    the caller has already decided WHICH rows lose validity — keeping the decision
    in the policy layer and the write here, like every other SSOT mutation."""
    removed = 0
    for token_id in token_ids:
        token = session.get(Token, token_id)
        if token is not None:
            session.delete(token)
            removed += 1
    if removed:
        session.commit()
    return removed


def delete_token(
    session: Session,
    token_id: str,
    *,
    protect_last_control: Callable[[Session], bool] | None = None,
) -> str:
    """Delete a token; returns 'deleted', 'not_found', or 'last_control'.

    When ``protect_last_control`` is given, the row is removed and the write lock taken
    (``flush``) *before* the predicate runs, so a concurrent settings change that just
    turned enforcement on is already visible (SQLite serializes writers). If the
    predicate then returns True and no ADMIN-capable control token would remain
    (``admin_credential_exists`` — a member's login token is also ``control``-scoped
    but must NOT satisfy the guard, or deleting the last admin credential would strand
    the box with only member logins), the transaction is rolled back and 'last_control'
    is returned. This keeps the last admin credential from being deleted out from under
    enforcement, both for two concurrent deletes and for a delete racing a settings
    change that enables enforcement."""
    token = session.get(Token, token_id)
    if token is None:
        return "not_found"
    session.delete(token)
    if token.scope == "control" and protect_last_control is not None:
        session.flush()  # take the write lock before re-reading enforcement state
        if protect_last_control(session) and not admin_credential_exists(session):
            session.rollback()
            return "last_control"
    session.commit()
    return "deleted"


def control_token_exists(session: Session) -> bool:
    return session.exec(select(Token).where(Token.scope == "control")).first() is not None


# --------------------------------------------------------------------------- #
# users (control-plane identities)
# --------------------------------------------------------------------------- #


def create_user(session: Session, user: User) -> User:
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def get_user(session: Session, user_id: str) -> Optional[User]:
    return session.get(User, user_id)


def list_users(session: Session) -> list[User]:
    return list(session.exec(select(User).order_by(User.created_at)).all())


def save_user(session: Session, user: User) -> User:
    user.updated_at = utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def count_servers_owned(session: Session, user_id: str) -> int:
    return len(session.exec(select(Server.id).where(Server.owner_id == user_id)).all())


def delete_user_and_tokens(session: Session, user_id: str) -> bool:
    """Delete a user and revoke EVERY token bound to them (control credentials and
    data-plane tokens alike) in one transaction, so a removed identity can't keep
    authenticating. The caller has already refused the delete while the user owns
    servers, so no Server.owner_id can dangle. Returns False when the user didn't
    exist (idempotent for a retried delete)."""
    user = session.get(User, user_id)
    if user is None:
        return False
    for token in session.exec(select(Token).where(Token.user_id == user_id)).all():
        session.delete(token)
    session.delete(user)
    session.commit()
    return True


def admin_credential_exists(session: Session, *, excluding_user_id: Optional[str] = None) -> bool:
    """Is there at least one usable ADMIN login besides ``excluding_user_id``'s?
    True when a control token exists that resolves to admin: one with no user
    (legacy/boot mint) or one belonging to an admin user. The users API consults
    this before demoting or deleting an admin so the last admin credential can't
    be removed (MCPE_ADMIN_TOKEN, checked by the caller, always lifts the guard)."""
    for token in session.exec(select(Token).where(Token.scope == "control")).all():
        if token.user_id is None:
            return True
        if excluding_user_id is not None and token.user_id == excluding_user_id:
            continue
        user = session.get(User, token.user_id)
        if user is not None and user.role == "admin":
            return True
    return False
