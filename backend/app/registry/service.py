"""Desired-state CRUD for MCP servers.

Sits above the repo: generates identity (id/slug), computes the idempotency
``config_hash``, validates the runner, and owns the import/export of the standard
``mcpServers`` JSON shape. Never spawns processes — that's the reconciler's job.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel import Session

from app.db import repo
from app.db.models import RUNNERS, Server
from app.util import config_hash, new_id, slugify


def compute_hash(server: Server) -> str:
    return config_hash(
        {
            "runner": server.runner,
            "command": server.command,
            "args": server.args,
            "env": server.env,
            "cwd": server.cwd,
            "mcp_http": server.mcp_http,
            "rest_openapi": server.rest_openapi,
            # auth_provider is intentionally excluded: it's enforced at the proxy
            # per-request, so changing it must NOT restart the bridge process.
        }
    )


def backfill_config_hashes(session: Session) -> int:
    """Recompute ``config_hash`` for stored servers so rows written by an older
    version (with a different hash-input shape — e.g. ``auth_provider`` was once
    included) are rehashed to the current shape. Without this, the first
    non-hash-affecting PATCH on an upgraded server would change the stored hash and
    trigger a spurious bridge restart. Idempotent — only writes rows whose hash
    actually changed. Returns how many were updated."""
    changed = 0
    for server in repo.list_servers(session):
        new_hash = compute_hash(server)
        if new_hash != server.config_hash:
            repo.set_config_hash(session, server.id, new_hash)
            changed += 1
    return changed


_AUTH_PROVIDERS = {"inherit", "none", "bearer"}


def normalize_auth_providers(session: Session) -> int:
    """Canonicalize stored ``auth_provider`` values from older versions (the old API
    schema accepted any ``str``): ``"Bearer"`` / ``"bearer "`` -> ``"bearer"``, and an
    unresolvable value -> ``"inherit"`` (the admin-controlled default). Without this,
    the dashboard/copy snippets advertise a usable endpoint while ``resolve()`` 403s
    the raw value on every ``/s/...`` request. Idempotent. Returns the count changed."""
    changed = 0
    for server in repo.list_servers(session):
        norm = (server.auth_provider or "").strip().lower()
        if norm not in _AUTH_PROVIDERS:
            norm = "inherit"
        if norm != server.auth_provider:
            repo.set_auth_provider(session, server.id, norm)
            changed += 1
    return changed


def _unique_slug(session: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while repo.get_server_by_slug(session, slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_server(
    session: Session,
    *,
    name: str,
    runner: str,
    command: str,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    mcp_http: bool = True,
    rest_openapi: bool = False,
    auth_provider: str = "inherit",
    enabled: bool = False,
    source: str = "manual",
) -> Server:
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r}; must be one of {RUNNERS}")
    if not command.strip():
        raise ValueError("command is required")

    server = Server(
        id=new_id(),
        slug=_unique_slug(session, name),
        name=name.strip() or "server",
        runner=runner,
        command=command.strip(),
        args=list(args or []),
        env=dict(env or {}),
        cwd=cwd,
        mcp_http=mcp_http,
        rest_openapi=rest_openapi,
        auth_provider=auth_provider,
        enabled=enabled,
        source=source,
    )
    server.config_hash = compute_hash(server)
    return repo.create_server(session, server)


_MUTABLE_FIELDS = {
    "name",
    "runner",
    "command",
    "args",
    "env",
    "cwd",
    "mcp_http",
    "rest_openapi",
    "auth_provider",
}


def update_server(session: Session, server_id: str, changes: dict[str, Any]) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    for key, value in changes.items():
        if key in _MUTABLE_FIELDS:
            setattr(server, key, value)
    if server.runner not in RUNNERS:
        raise ValueError(f"unknown runner {server.runner!r}")
    server.config_hash = compute_hash(server)  # recompute -> drives idempotent reconcile
    return repo.save_server(session, server)


def set_enabled(session: Session, server_id: str, enabled: bool) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    server.enabled = enabled
    return repo.save_server(session, server)


# Node/Python launchers we recognize so the runner badge is meaningful. Anything
# else is stored as a generic `command` (still launched verbatim).
_NPX_LAUNCHERS = {"npx", "npx.cmd", "bunx", "pnpm", "node"}
_UVX_LAUNCHERS = {"uvx", "uv"}


def _infer_runner(command: str) -> str:
    base = command.strip().lower()
    if base in _NPX_LAUNCHERS:
        return "npx"
    if base in _UVX_LAUNCHERS:
        return "uvx"
    if base == "docker":
        return "docker"
    return "command"


def import_mcp_servers(session: Session, data: dict) -> tuple[list[Server], list[dict]]:
    """Create servers from the standard Claude-Desktop ``mcpServers`` JSON shape.

    Accepts either ``{"mcpServers": {...}}`` or a bare ``{name: {...}}`` map.
    Stdio entries (``command`` + ``args`` + ``env``) become servers, stored
    verbatim and disabled (the user reviews, then enables). Already-remote
    entries (``url`` / ``type: sse|streamable-http``) are skipped — they're
    nothing to elevate.
    """
    servers_map = data.get("mcpServers") if isinstance(data, dict) and "mcpServers" in data else data
    if not isinstance(servers_map, dict):
        raise ValueError("expected an object with an 'mcpServers' map of servers")

    created: list[Server] = []
    skipped: list[dict] = []
    for name, entry in servers_map.items():
        if not isinstance(entry, dict):
            skipped.append({"name": str(name), "reason": "entry is not an object"})
            continue
        if entry.get("url") or entry.get("type") in ("sse", "streamable-http", "http"):
            skipped.append({"name": str(name), "reason": "already a remote HTTP server"})
            continue
        command = entry.get("command")
        if not command:
            skipped.append({"name": str(name), "reason": "no command to launch"})
            continue
        try:
            created.append(
                create_server(
                    session,
                    name=str(name),
                    runner=_infer_runner(command),
                    command=str(command),
                    args=list(entry.get("args") or []),
                    env=dict(entry.get("env") or {}),
                    source="import",
                    enabled=False,
                )
            )
        except ValueError as exc:
            skipped.append({"name": str(name), "reason": str(exc)})
    return created, skipped
