"""Typed CRUD — the ONLY module that writes SSOT rows (INSERT/UPDATE/DELETE).

Keeping writes funneled through here enforces SSOT at the code level: services
and the reconciler read freely, but mutate only via these functions.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlmodel import Session, select

from app.db.models import Server, ServerRuntime, Setting, utcnow

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
