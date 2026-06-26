"""Typed CRUD — the ONLY module that writes SSOT rows (INSERT/UPDATE/DELETE).

Keeping writes funneled through here enforces SSOT at the code level: services
and the reconciler read freely, but mutate only via these functions.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.models import Server, ServerRuntime, Setting, Token, utcnow

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
            state="stopped", pid=None, port=None, last_error=None, tools=[], updated_at=utcnow()
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


def setting_set(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    encoded = json.dumps(value)
    if row is None:
        row = Setting(key=key, value=encoded)
    else:
        row.value = encoded
    session.add(row)
    session.commit()


def setting_set_many(session: Session, items: dict[str, Any]) -> None:
    """Set several settings atomically (one commit), so a multi-field settings
    patch never partially applies. The caller validates before calling."""
    for key, value in items.items():
        row = session.get(Setting, key)
        encoded = json.dumps(value)
        if row is None:
            row = Setting(key=key, value=encoded)
        else:
            row.value = encoded
        session.add(row)
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


def delete_token(session: Session, token_id: str) -> bool:
    token = session.get(Token, token_id)
    if token is None:
        return False
    session.delete(token)
    session.commit()
    return True


def control_token_exists(session: Session) -> bool:
    return session.exec(select(Token).where(Token.scope == "control")).first() is not None


def count_control_tokens(session: Session) -> int:
    return len(session.exec(select(Token).where(Token.scope == "control")).all())
