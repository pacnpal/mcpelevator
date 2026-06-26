"""SQLite SSOT schema.

Design note: **desired state** (``Server``) is strictly separated from **observed
runtime state** (``ServerRuntime``). The reconciler owns all writes to runtime;
on boot, runtime rows are treated as stale and re-derived from desired state.

v1 simplification (don't over-engineer): the 1:1 exposure config is folded into
the ``Server`` row (``mcp_http`` / ``rest_openapi`` / ``auth_provider``) rather
than a separate table. A separate table is only needed if exposure becomes 1:N.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- enums kept as plain string constants for deterministic SQLite storage ---

RUNNERS = ("npx", "uvx", "command", "docker")
STATES = ("stopped", "starting", "running", "unhealthy", "failed", "stopping")


class Server(SQLModel, table=True):
    """Desired state + identity. The thing the user defines and toggles."""

    __tablename__ = "server"

    id: str = Field(primary_key=True)
    slug: str = Field(index=True, unique=True)  # url-safe, immutable identity in /s/<slug>/
    name: str

    # launch spec
    runner: str = "npx"  # one of RUNNERS
    command: str = ""  # package name / image ref / argv0
    args: list = Field(sa_column=Column(JSON))  # JSON array; always set by service
    env: dict = Field(sa_column=Column(JSON))  # JSON object; always set by service
    cwd: Optional[str] = None

    # exposure (folded 1:1)
    mcp_http: bool = True
    rest_openapi: bool = False
    auth_provider: str = "inherit"  # inherit | none | bearer

    # desired runtime
    enabled: bool = False
    config_hash: str = ""  # idempotency anchor
    source: str = "manual"  # manual | import | catalog:<id>

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ServerRuntime(SQLModel, table=True):
    """Observed state, reconciler-owned. Persisted only so the UI shows truth
    across control-plane restarts; never authoritative."""

    __tablename__ = "server_runtime"

    server_id: str = Field(primary_key=True, foreign_key="server.id")
    state: str = "stopped"  # one of STATES
    pid: Optional[int] = None
    port: Optional[int] = None
    last_error: Optional[str] = None
    restart_count: int = 0
    last_health: Optional[datetime] = None
    tools: list = Field(sa_column=Column(JSON))  # cached [{name, description}] for the UI
    updated_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    """Runtime-mutable key/value settings (JSON-encoded values)."""

    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str  # JSON-encoded


class Token(SQLModel, table=True):
    """A bearer token. Only the SHA-256 hash is stored; the plaintext is shown
    to the user exactly once at creation. Revoking = hard delete (v1)."""

    __tablename__ = "token"

    id: str = Field(primary_key=True)
    name: str
    token_hash: str = Field(index=True)
    prefix: str  # first chars of the plaintext, for UI identification only
    # "all" authorizes every bearer-protected server; a server.id restricts the
    # token to that one server (enforced in app/auth/bearer.py).
    scope: str = "all"
    created_at: datetime = Field(default_factory=utcnow)
