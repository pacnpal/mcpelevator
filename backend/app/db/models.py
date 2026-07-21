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

RUNNERS = ("npx", "uvx", "command", "docker", "remote")
# Control-plane roles. "admin" sees and manages everything; "member" sees only the
# servers/tokens they own. Plain strings (not Enum) for deterministic SQLite storage.
ROLES = ("admin", "member")
# "idle" is an enabled server quiesced by the supervisor after its idle timeout;
# the proxy reactivates it on the next request (wake-on-request).
STATES = ("stopped", "starting", "running", "unhealthy", "failed", "stopping", "idle")


class User(SQLModel, table=True):
    """A control-plane identity. Users don't hold passwords — they hold control
    tokens (``Token.user_id``), minted by an admin and pasted at login, so the
    existing bearer machinery is the whole credential story. ``role`` decides
    reach: an admin sees everything; a member sees only what they own.

    ``local_runners`` gates whether the user may configure servers that execute
    code on this box (npx/uvx/command/docker). Local runners run as the
    mcpelevator process user, so this is an authorization line, NOT an isolation
    boundary — multi-user assumes mutually trusting users (see README Security).
    """

    __tablename__ = "user"

    id: str = Field(primary_key=True)
    name: str
    role: str = "member"  # one of ROLES
    local_runners: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Server(SQLModel, table=True):
    """Desired state + identity. The thing the user defines and toggles."""

    __tablename__ = "server"

    id: str = Field(primary_key=True)
    slug: str = Field(index=True, unique=True)  # url-safe routing key in /s/<slug>/ (operator-renameable)
    name: str

    # launch spec
    runner: str = "npx"  # one of RUNNERS
    command: str = ""  # package name / image ref / argv0 / upstream URL (remote)
    # JSON array; always set by service. For runner="remote" this is [transport],
    # e.g. ["streamable-http"] or ["sse"].
    args: list = Field(sa_column=Column(JSON))
    # JSON object; always set by service. For runner="remote" this is the upstream
    # HTTP headers (e.g. {"Authorization": "Bearer …"}), not process env.
    env: dict = Field(sa_column=Column(JSON))
    # JSON array; runner="docker" only — extra `docker run` options placed before the
    # image (e.g. --name, --shm-size=1g), validated at the service boundary
    # (runners.docker.run_args_error) and forced [] for every other runner. Nullable:
    # rows predating the column hold NULL, read as [].
    run_args: Optional[list] = Field(default=None, sa_column=Column(JSON))
    cwd: Optional[str] = None
    setup_script: str = ""

    # JSON array of upstream tool names to hide from every exposed surface (MCP
    # tools/list + call, the REST/OpenAPI routes, and the group hub, which all funnel
    # through the bridge). Empty/NULL = expose every discovered tool (the default).
    # A hidden tool is dropped from listings AND refused on call, so a client holding a
    # stale list can't still invoke it. Part of the launch spec (config_hash), so a
    # change restarts the bridge — the reconciler re-applies the filter deterministically.
    # Nullable: rows predating the column hold NULL, read as [].
    disabled_tools: Optional[list] = Field(default=None, sa_column=Column(JSON))

    # exposure (folded 1:1)
    mcp_http: bool = True
    rest_openapi: bool = False
    auth_provider: str = "inherit"  # inherit | none | bearer

    # upstream OAuth (runner="remote" only). When set, mcpelevator authenticates to
    # the upstream via an OAuth 2.1 authorization-code grant instead of the static
    # `env` headers. The obtained tokens/DCR client info live in a file store
    # (app.auth.oauth_store), NOT here — so authenticating never re-hashes the row or
    # bounces the bridge. `oauth_client_id`/`oauth_client_secret` are optional static
    # client credentials; empty means Dynamic Client Registration.
    oauth: bool = False
    oauth_scopes: str = ""  # space-separated scopes to request (empty = server default)
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None

    # Idle quiescence: seconds of no proxy traffic before the supervisor stops the
    # bridge (state "idle") and the proxy restarts it on the next request. NULL =
    # inherit the `idle_timeout_s` runtime setting; 0 = never idle this server.
    # Excluded from config_hash — changing it must not bounce a running bridge.
    idle_timeout_s: Optional[int] = None

    # desired runtime
    enabled: bool = False
    config_hash: str = ""  # idempotency anchor
    source: str = "manual"  # manual | import | catalog:<id>

    # Ownership: the User who created (or was assigned) this server. NULL means
    # admin-owned — the deterministic upgrade default (pre-multi-user rows migrate
    # to NULL), visible to admins only. Excluded from config_hash — reassigning an
    # owner must not bounce a running bridge. Indexed: every member request
    # filters on it (visibility), as does count_servers_owned.
    owner_id: Optional[str] = Field(default=None, index=True)

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
    # cached [{name, description, has_output_schema}] for the UI (see unit.tool_summary)
    tools: list = Field(sa_column=Column(JSON))
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
    # The User this token belongs to. For scope="control" this IS the login
    # credential's identity: NULL resolves to admin (legacy tokens minted before
    # multi-user, and boot/recovery mints, keep full power). For data-plane scopes
    # it records who minted the token, so members manage only their own. Indexed:
    # token visibility and per-user revocation sweeps filter on it.
    user_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
