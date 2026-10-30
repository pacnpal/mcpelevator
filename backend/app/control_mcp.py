"""The control plane as an MCP server, served at ``/api/mcp``.

Clients normally reach it as the built-in ``mcpelevator`` server (``/s/<slug>/mcp``,
provisioned on boot by ``service.ensure_builtin``): an ordinary remote bridge in front
of this endpoint, so per-tool hiding/renaming, REST, auth provider, idle, usage and
logs all apply as for any server. That bridge forwards the caller's Authorization and
adds a per-process internal header (``app.runners.builtin``). The row's ``enabled``
flag is the global switch (also exposed as the ``control_mcp`` setting) and a user's
``control_mcp`` flag the per-user one; both are checked here on every request.

Every tool is a thin wrapper over one ``/api`` route, called IN-PROCESS (an
``httpx.ASGITransport`` onto this same FastAPI app) with the caller's own headers
and peer address. So an agent driving mcpelevator gets exactly what the UI gets:
the same Host/Origin allowlist, the same control-token gate, the same per-user
visibility and role checks (``principal.resolve`` / ``policy``). Nothing here
re-derives a rule. A refused call surfaces as a tool error carrying the API's
status and detail.

The one exception is ``get_server_logs``: the API's log route is a never-ending
SSE stream, so the tool checks visibility through ``GET /api/servers/{id}`` and
then reads the unit's bounded backlog directly.

The endpoint itself is gated (``_authorize``: the same credentials as the sensitive routers)
before the MCP app sees the request, so an unauthenticated client can't even list
tools once enforcement is on. The exception is the built-in bridge (internal header,
no Authorization), which is how the supervisor's discovery probe lists the tools;
every tool call still needs the caller's own credential. It runs stateless.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Annotated, Any, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from pydantic import Field
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.api import servers as servers_api
from app.api.schemas import GroupMembers, Role, ServerCreate, ServerUpdate, SettingsUpdate
from app.auth import principal as principal_mod
from app.auth import cf_access
from app.auth.control_plane import enforcement_enabled, is_env_admin_token
from app.db import get_engine, repo
from app.groups import registry
from app.registry import service
from app.util import hash_token
from app.runners.builtin import (
    GROUP_HEADER,
    INSTRUCTIONS,
    ORIGINAL_HOST_HEADER,
    PATH,
    localize,
)
from app.runners.builtin import is_internal as _internal
from app.runners.remote import canonical_transport

# Request headers that describe the MCP transport, not the caller. Everything
# else (authorization, host, origin, forwarding headers) is passed through so the
# inner /api request is judged exactly as the outer one was.
_TRANSPORT_HEADERS = {
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "content-type",
    "last-event-id",
    "transfer-encoding",
}

def _seg(value: str) -> str:
    """A tool argument as ONE path segment. Dots too: httpx would resolve a literal
    ``..`` and send ``delete_server("../tokens/x")`` to a different route."""
    return quote(str(value), safe="").replace(".", "%2E")


_READ = {"readOnlyHint": True, "openWorldHint": False}
# Purely additive writes (create, start). Anything that overwrites or stops uses
# _WRITE, which leaves destructiveHint out: absent means "may be destructive".
_ADD = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}



def build(app: FastAPI) -> "_Gate":
    """The gated ``/api/mcp`` ASGI app for ``app``. The inner MCP app (``.inner``) has
    a lifespan the host app must run (see ``main.lifespan``)."""
    mcp = FastMCP("mcpelevator", instructions=INSTRUCTIONS, version=__version__)

    async def api(
        method: str, path: str, *, json: Any = None, params: dict | None = None, raw: bool = False
    ) -> Any:
        outer = get_http_request()
        headers = {
            k: v for k, v in outer.headers.items()
            if k not in _TRANSPORT_HEADERS and k != ORIGINAL_HOST_HEADER
        }
        original_host = outer.headers.get(ORIGINAL_HOST_HEADER)
        if original_host and _internal(outer):
            headers["host"] = original_host  # see app.runners.builtin.ORIGINAL_HOST_HEADER
        peer = (outer.client.host, outer.client.port) if outer.client else ("127.0.0.1", 0)
        transport = httpx.ASGITransport(app=app, client=peer)
        query = {k: v for k, v in (params or {}).items() if v is not None}
        async with httpx.AsyncClient(transport=transport, base_url="http://mcpelevator") as client:
            r = await client.request(method, path, json=json, params=query, headers=headers)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ToolError(f"{r.status_code}: {detail}")
        if raw:
            return r.content
        if not r.content:
            return {"ok": True}
        data = r.json()
        # A bare list would reach the client as one content block per item (none when
        # empty); one JSON object is what every tool result should be.
        return {"items": data} if isinstance(data, list) else data

    # ---- Instance -----------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def whoami() -> Any:
        """Who this connection acts as: user id, name, role, local-runner permission, and
        whether control-plane auth is enforced."""
        return await api("GET", "/api/auth/status")

    @mcp.tool(annotations=_READ)
    async def health_summary() -> Any:
        """Instance health: version and a per-state count of servers."""
        return await api("GET", "/api/health/summary")

    @mcp.tool(annotations=_READ)
    async def server_health(slug: str) -> Any:
        """Health of one server by slug (its live state and readiness)."""
        return await api("GET", f"/api/health/{_seg(slug)}")

    @mcp.tool(annotations=_READ)
    async def get_settings() -> Any:
        """Runtime settings: bind mode, allowed hosts, default auth provider,
        control-plane auth, idle timeout, usage retention, OAuth, docker runner."""
        return await api("GET", "/api/settings")

    @mcp.tool(annotations=_WRITE)
    async def update_settings(changes: SettingsUpdate) -> Any:
        """Change runtime settings (admin only). Only the fields given change. A change
        that would turn on control-plane auth without an admin credential is refused."""
        if changes.control_mcp is False and _internal(get_http_request()):
            # Turning control_mcp off disables the built-in server, i.e. the bridge
            # carrying this call (see _refuse_self).
            raise ToolError(
                "the built-in server can't turn itself off through its own connection; "
                "use Settings or PATCH /api/settings"
            )
        return await api("PATCH", "/api/settings", json=changes.model_dump(exclude_unset=True))

    @mcp.tool(annotations=_READ)
    async def get_usage(days: Annotated[int, Field(ge=1)] = 7) -> Any:
        """Instance-wide tool-call counts over the trailing ``days``: totals, a series,
        and per-server / per-tool rollups (only servers visible to the caller)."""
        return await api("GET", "/api/usage", params={"days": days})

    # ---- Servers ------------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_servers() -> Any:
        """Every server visible to the caller, with id, slug, runner, enabled flag,
        live state, endpoint URLs, and discovered tools."""
        return await api("GET", "/api/servers")

    @mcp.tool(annotations=_READ)
    async def get_server(server_id: str) -> Any:
        """One server's full detail: launch spec, live state, startup status, OAuth
        status, tools, and its /s/<slug> URLs."""
        return await api("GET", f"/api/servers/{_seg(server_id)}")

    @mcp.tool(annotations=_ADD)
    async def create_server(spec: ServerCreate) -> Any:
        """Register a server. ``runner`` is one of npx, uvx, command, remote (``command``
        is then the upstream MCP URL), or docker (``command`` is the image). Created
        disabled unless ``enabled`` is true. A local runner can launch from a git checkout:
        ``repo_url`` (+ optional ``repo_ref``) is cloned, ``post_clone_script`` runs in it
        when the commit or script changes, and the command runs inside it."""
        return await api("POST", "/api/servers", json=spec.model_dump(exclude_unset=True))

    @mcp.tool(annotations=_WRITE)
    async def update_server(server_id: str, changes: ServerUpdate) -> Any:
        """Edit a server; only the fields given change (an explicit null resets the
        nullable ones to inherit). A launch-spec change restarts its bridge. That includes
        ``repo_url``/``repo_ref``/``post_clone_script`` (a changed script or commit
        rebuilds on the next start; ``repo_url: ""`` drops the checkout)."""
        if changes.model_fields_set & _RESTARTING_FIELDS:
            _refuse_self(server_id, "change its exposure or tool policy")
        return await api(
            "PATCH", f"/api/servers/{_seg(server_id)}", json=changes.model_dump(exclude_unset=True)
        )

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_server(server_id: str) -> Any:
        """Stop and permanently delete a server, its tokens, and its group memberships."""
        return await api("DELETE", f"/api/servers/{_seg(server_id)}")

    @mcp.tool(annotations=_ADD)
    async def clone_server(server_id: str, name: Optional[str] = None) -> Any:
        """Copy a server's configuration into a new, disabled server."""
        return await api("POST", f"/api/servers/{_seg(server_id)}/clone", json={"name": name})

    @mcp.tool(annotations=_ADD)
    async def import_servers(config: dict) -> Any:
        """Bulk-create from a standard ``{"mcpServers": {...}}`` config (as used by
        Claude Desktop and others). Imported servers start disabled."""
        return await api("POST", "/api/servers/import", json=config)

    @mcp.tool(annotations=_ADD)
    async def enable_server(server_id: str) -> Any:
        """Set a server desired-running; the supervisor starts it."""
        return await api("POST", f"/api/servers/{_seg(server_id)}/enable")

    @mcp.tool(annotations=_WRITE)
    async def disable_server(server_id: str) -> Any:
        """Set a server desired-stopped; the supervisor stops it."""
        _refuse_self(server_id, "disable")
        return await api("POST", f"/api/servers/{_seg(server_id)}/disable")

    @mcp.tool(annotations=_ADD)
    async def retry_server(server_id: str) -> Any:
        """Start a fresh activation of a failed server."""
        return await api("POST", f"/api/servers/{_seg(server_id)}/retry")

    @mcp.tool(annotations=_WRITE)
    async def restart_server(server_id: str) -> Any:
        """Restart an enabled server's bridge from unchanged config (re-discovers tools)."""
        _refuse_self(server_id, "restart")
        return await api("POST", f"/api/servers/{_seg(server_id)}/restart")

    # No destructiveHint: whether a call destroys anything depends on the tool chosen,
    # and an absent hint means "may be destructive" to the client.
    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def call_server_tool(
        server_id: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout_s: Annotated[float, Field(ge=1, le=300)] = 60,
    ) -> Any:
        """Invoke one tool on a running server (the UI playground; not counted as
        usage). A tool's own failure comes back as ``is_error`` in the result."""
        return await api(
            "POST",
            f"/api/servers/{_seg(server_id)}/tools/{_seg(tool_name)}/call",
            json={"arguments": arguments or {}, "timeout_s": timeout_s},
        )

    @mcp.tool(annotations=_READ)
    async def get_server_logs(
        server_id: str, tail: Annotated[int, Field(ge=1, le=2000)] = 200
    ) -> Any:
        """The last ``tail`` lines of a server's current activation log (setup script,
        bridge, and upstream stderr). It's the first place to look when it failed."""
        await api("GET", f"/api/servers/{_seg(server_id)}")  # visibility: 404s like the route
        unit = app.state.supervisor.unit(server_id)
        if unit is None:
            return {"running": False, "lines": []}
        lines = unit.logs.snapshot()[-tail:]
        # Ownership can move during the await above. Re-check with no await in between,
        # like the SSE route does before its first byte.
        with Session(get_engine()) as session:
            principal = principal_mod.resolve(get_http_request(), session)
        if principal is None or not servers_api._visible_now(server_id, principal):
            raise ToolError("404: server not found")
        # A retained failed/unhealthy unit still has logs but isn't serving.
        return {"running": unit.state == "running", "state": unit.state, "lines": lines}

    @mcp.tool(annotations=_READ)
    async def wait_for_server(
        server_id: str, timeout_s: Annotated[float, Field(ge=1, le=300)] = 60
    ) -> Any:
        """Wait until a server leaves starting/stopping (after create, enable, restart or
        an edit), then return its detail. ``timed_out`` is true if it was still settling."""
        deadline = time.monotonic() + timeout_s
        while True:
            detail = await api("GET", f"/api/servers/{_seg(server_id)}")
            settling = detail.get("state") in ("starting", "stopping")
            if not settling or time.monotonic() >= deadline:
                return {**detail, "timed_out": settling}
            await asyncio.sleep(1)

    @mcp.tool(annotations=_READ)
    async def export_server_mcpb(server_id: str) -> Any:
        """A local server's launch spec as a .mcpb bundle (Claude Desktop extension),
        base64-encoded. Remote servers can't be exported."""
        data = await api("GET", f"/api/servers/{_seg(server_id)}/mcpb", raw=True)
        detail = await api("GET", f"/api/servers/{_seg(server_id)}")
        return {
            "filename": f"{detail['slug']}.mcpb",
            "content_base64": base64.b64encode(data).decode(),
        }

    @mcp.tool(annotations=_READ)
    async def get_server_usage(server_id: str, days: Annotated[int, Field(ge=1)] = 7) -> Any:
        """One server's tool-call counts over the trailing ``days``: totals, per tool,
        and a series."""
        return await api("GET", f"/api/servers/{_seg(server_id)}/usage", params={"days": days})

    @mcp.tool(annotations=_ADD)
    async def start_server_oauth(server_id: str) -> Any:
        """Begin the upstream OAuth sign-in for a remote server with ``oauth`` on.
        Returns the authorization URL a human must open in a browser to finish it."""
        return await api("POST", f"/api/servers/{_seg(server_id)}/oauth/authorize")

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def disconnect_server_oauth(server_id: str) -> Any:
        """Forget a server's stored upstream OAuth tokens."""
        return await api("POST", f"/api/servers/{_seg(server_id)}/oauth/disconnect")

    # ---- Groups (admin) -----------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_groups() -> Any:
        """Every group with its members ("*" or server ids) and its /g/<name>/mcp URL."""
        return await api("GET", "/api/groups")

    @mcp.tool(annotations=_WRITE)
    async def put_group(name: str, members: GroupMembers) -> Any:
        """Create or replace a group: ``members`` is "*" (every server, present and
        future) or an ordered list of server ids. Tools surface as <slug>_<tool>."""
        return await api("PUT", f"/api/groups/{_seg(name)}", json={"members": members})

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_group(name: str) -> Any:
        """Delete a group and revoke the tokens scoped to it."""
        return await api("DELETE", f"/api/groups/{_seg(name)}")

    @mcp.tool(annotations=_WRITE)
    async def restart_group(name: str) -> Any:
        """Restart every enabled member of a group; reports restarted/skipped/failed."""
        if _internal(get_http_request()):
            with Session(get_engine()) as session:
                if registry.includes_builtin(session, name):
                    raise ToolError(
                        f"group {name!r} includes the built-in server, which can't restart "
                        "itself through its own connection; use the dashboard or the /api route"
                    )
        return await api("POST", f"/api/groups/{_seg(name)}/restart")

    # ---- Tokens -------------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_tokens() -> Any:
        """Tokens visible to the caller (prefix and scope only, never the secret)."""
        return await api("GET", "/api/tokens")

    @mcp.tool(annotations=_ADD)
    async def create_token(name: str, scope: str = "all") -> Any:
        """Mint a token. ``scope``: "all" (every bearer-protected server), a server id,
        "group:<name>", or "control" (control plane). The plaintext is returned once."""
        return await api("POST", "/api/tokens", json={"name": name, "scope": scope})

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_token(token_id: str) -> Any:
        """Revoke a token."""
        return await api("DELETE", f"/api/tokens/{_seg(token_id)}")

    # ---- Users (admin) ------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_users() -> Any:
        """Every user with role, local-runner permission, and owned server/token counts."""
        return await api("GET", "/api/users")

    @mcp.tool(annotations=_ADD)
    async def create_user(
        name: str, role: Role = "member", local_runners: bool = False, control_mcp: bool = True
    ) -> Any:
        """Create a user. ``local_runners`` lets a member use npx/uvx/command (code
        execution on this host); ``control_mcp`` lets them use this MCP server. Mint
        their login with create_user_credential."""
        payload = {
            "name": name, "role": role, "local_runners": local_runners, "control_mcp": control_mcp
        }
        return await api("POST", "/api/users", json=payload)

    @mcp.tool(annotations=_WRITE)
    async def update_user(
        user_id: str,
        name: Optional[str] = None,
        role: Optional[Role] = None,
        local_runners: Optional[bool] = None,
        control_mcp: Optional[bool] = None,
    ) -> Any:
        """Rename a user or change their role, local-runner permission, or access to this
        MCP server."""
        changes = {
            "name": name, "role": role, "local_runners": local_runners, "control_mcp": control_mcp
        }
        return await api(
            "PATCH", f"/api/users/{_seg(user_id)}", json={k: v for k, v in changes.items() if v is not None}
        )

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_user(user_id: str) -> Any:
        """Delete a user and their tokens (refused while they still own servers)."""
        return await api("DELETE", f"/api/users/{_seg(user_id)}")

    @mcp.tool(annotations=_ADD)
    async def create_user_credential(user_id: str) -> Any:
        """Mint a control-plane login token for a user; the plaintext is returned once."""
        return await api("POST", f"/api/users/{_seg(user_id)}/credentials")

    # ---- Catalog ------------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_catalog_sources() -> Any:
        """The public MCP directories the catalog can browse."""
        return await api("GET", "/api/catalog/sources")

    @mcp.tool(annotations={**_READ, "openWorldHint": True})
    async def search_catalog(
        search: Optional[str] = None,
        source: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Annotated[Optional[int], Field(ge=1, le=100)] = None,
    ) -> Any:
        """Search a public MCP directory; page with the returned cursor."""
        return await api(
            "GET",
            "/api/catalog/servers",
            params={"search": search, "source": source, "cursor": cursor, "limit": limit},
        )

    @mcp.tool(annotations={**_READ, "openWorldHint": True})
    async def get_catalog_server(
        id: str, source: Optional[str] = None, version: str = "latest"
    ) -> Any:
        """A catalog entry's detail, including a reviewable launch spec to pass to
        create_server."""
        return await api(
            "GET", "/api/catalog/server", params={"id": id, "source": source, "version": version}
        )

    @mcp.tool(annotations={**_READ, "openWorldHint": True})
    async def list_catalog_versions(id: str, source: Optional[str] = None) -> Any:
        """The published versions of a catalog entry."""
        return await api("GET", "/api/catalog/server/versions", params={"id": id, "source": source})

    @mcp.tool(annotations={**_READ, "openWorldHint": True})
    async def get_repository_draft(url: str) -> Any:
        """Resolve a GitHub repository URL (a server distributed as source, not on npm or
        PyPI) into reviewable drafts: repo_url/repo_ref, a detected post_clone_script
        (install + build) and the start command run inside the checkout. Anything that
        couldn't be detected is blank with a ``reason``. Edit as needed and pass to
        create_server."""
        return await api("GET", "/api/catalog/repository", params={"url": url})

    @mcp.tool(annotations={**_ADD, "openWorldHint": True})
    async def install_from_repository(
        url: str,
        name: Optional[str] = None,
        post_clone_script: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        enabled: Optional[bool] = None,
    ) -> Any:
        """Install a server from a GitHub repository in one step: mcpelevator clones it,
        runs the post-clone script (install + build) and launches the command inside the
        checkout. Steps are auto-detected from package.json / pyproject.toml; pass
        ``post_clone_script``, ``command`` or ``args`` to override them. It starts only
        when a command is known, unless ``enabled`` says otherwise. Returns the server
        plus ``warnings`` and ``notes``; first start can take minutes (wait_for_server)."""
        detail = await api("GET", "/api/catalog/repository", params={"url": url})
        meta = detail["server"]
        draft = next((d for d in detail["drafts"] if d["installable"]), detail["drafts"][0])
        spec = {
            "runner": "command",
            "command": draft["command"] if command is None else command,
            "args": draft["args"] if args is None else args,
            "env": {**draft["env"], **(env or {})},
            "repo_url": draft["repo_url"],
            "repo_ref": draft["repo_ref"],
            "post_clone_script": (
                draft["post_clone_script"] if post_clone_script is None else post_clone_script
            ),
        }
        if not spec["command"].strip():
            raise ToolError(
                "couldn't detect how to start this repository; pass command (and args). "
                + " ".join([draft.get("reason") or ""] + detail.get("notes", []))
            )
        tag = f"#{meta['version']}" if meta.get("version") else ""
        created = await api(
            "POST",
            "/api/servers",
            json={
                **spec,
                "name": name or meta.get("title") or meta["name"],
                "source": f"github:{meta['name']}{tag}",
                "enabled": not draft["warnings"] if enabled is None else enabled,
            },
        )
        notes = list(detail.get("notes", []))
        if not draft["installable"] and draft.get("reason"):
            # What detection couldn't work out (no install/build steps, no start command)
            # — the caller supplied the command, but should know what else is blank.
            notes.append(draft["reason"])
        return {"server": created, "warnings": draft["warnings"], "notes": notes}

    @mcp.tool(annotations={**_ADD, "openWorldHint": True})
    async def install_from_catalog(
        id: str,
        source: Optional[str] = None,
        version: str = "latest",
        name: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        enabled: Optional[bool] = None,
    ) -> Any:
        """Install a catalog entry in one step, the way the UI's Install button does: the
        first installable package, else a proxyable remote endpoint. ``env`` fills in
        required values (env vars, or upstream headers for a remote). It starts only when
        nothing is left to fill in, unless ``enabled`` says otherwise. Returns the server
        plus any ``warnings`` (values still needed) and ``notes``."""
        detail = await api(
            "GET", "/api/catalog/server", params={"id": id, "source": source, "version": version}
        )
        meta = detail["server"]
        draft = next((d for d in detail["drafts"] if d["installable"]), None)
        remote = next(
            (r for r in detail["remotes"] if r.get("url") and canonical_transport(r["type"])),
            None,
        )
        if draft is not None:
            spec = {
                "runner": draft["runner"] or "npx",
                "command": draft["command"],
                "args": draft["args"],
                "env": {**draft["env"], **(env or {})},
            }
            warnings = draft["warnings"]
        elif remote is not None:
            spec = {
                "runner": "remote",
                "command": remote["url"],
                "args": [canonical_transport(remote["type"])],
                "env": {**remote["headers"], **(env or {})},
            }
            warnings = remote["warnings"]
        else:
            reasons = [d["reason"] for d in detail["drafts"] if d.get("reason")]
            raise ToolError(
                "no installable package or remote endpoint; use create_server by hand. "
                + " ".join(reasons + detail.get("notes", []))
            )
        # An env/header warning reads "Environment variable KEY ..." or "Header KEY ..."
        # (app.catalog.mapping); drop exactly those the caller just filled in.
        filled = tuple(
            f"{label} {k} "
            for k, v in (env or {}).items()
            if v
            for label in ("Environment variable", "Header")
        )
        warnings = [w for w in warnings if not w.startswith(filled)]
        tag = f"@{meta['version']}" if meta.get("version") else ""
        created = await api(
            "POST",
            "/api/servers",
            json={
                **spec,
                "name": name or meta.get("title") or meta["name"],
                "source": f"catalog:{meta['name']}{tag}",
                "enabled": not warnings if enabled is None else enabled,
            },
        )
        return {"server": created, "warnings": warnings, "notes": detail.get("notes", [])}

    # ---- Resources and prompts ---------------------------------------------

    @mcp.resource("mcpelevator://servers", mime_type="application/json")
    async def servers_resource() -> Any:
        """Every server visible to the caller."""
        return await api("GET", "/api/servers")

    @mcp.resource("mcpelevator://servers/{server_id}", mime_type="application/json")
    async def server_resource(server_id: str) -> Any:
        """One server's full detail."""
        return await api("GET", f"/api/servers/{_seg(server_id)}")

    @mcp.resource("mcpelevator://servers/{server_id}/logs", mime_type="text/plain")
    async def server_logs_resource(server_id: str) -> str:
        """A server's current activation log."""
        return "\n".join((await get_server_logs(server_id, 2000))["lines"])

    @mcp.resource("mcpelevator://groups", mime_type="application/json")
    async def groups_resource() -> Any:
        """Every group and its members (admin only)."""
        return await api("GET", "/api/groups")

    @mcp.resource("mcpelevator://settings", mime_type="application/json")
    async def settings_resource() -> Any:
        """The runtime settings."""
        return await api("GET", "/api/settings")

    async def _localized(text: str) -> str:
        # Prompts name tools; use the names this client actually sees. Only the bridge
        # applies the tool policy, so a direct /api/mcp client keeps the original names.
        # Through a group bundle every tool also carries this server's slug.
        request = get_http_request()
        if not _internal(request):
            return text
        with Session(get_engine()) as session:
            builtin = service.get_builtin(session)
        if builtin is None or not request.headers.get(GROUP_HEADER):
            return localize(text, builtin)
        tools = [tool.name for tool in await mcp.list_tools()]
        return localize(text, builtin, prefix=f"{builtin.slug}_", tools=tools)

    @mcp.prompt
    async def install_server(what: str) -> str:
        """Find and install an MCP server from the public catalog."""
        return await _localized(
            f"Install an MCP server for: {what}. Use search_catalog to find candidates and "
            "pick the best match (say why). Call install_from_catalog with its id. If it "
            "returns warnings, ask me for those values, then update_server with them and "
            "enable_server. Finish with wait_for_server and report its state, endpoint URL, "
            "and tools. If it failed, read get_server_logs and explain."
        )

    @mcp.prompt
    async def diagnose_server(server_id: str) -> str:
        """Work out why a server isn't running and fix what can be fixed."""
        return await _localized(
            f"Diagnose server {server_id}. Read get_server for its state, startup status and "
            "last error, and get_server_logs for the log. Explain the root cause in plain "
            "words. If it's a config problem (a missing env var, a wrong package or URL), "
            "propose the exact update_server change and apply it once I agree, then "
            "restart_server and wait_for_server to confirm."
        )

    # host_origin_protection off: the /api allowlist middleware already ran.
    inner = mcp.http_app(path=PATH, stateless_http=True, host_origin_protection=False)

    return _Gate(inner)


class _Gate:
    """``_authorize`` in front of the MCP app. A class, not a function:
    Starlette's ``Route`` treats a plain function endpoint as request -> response."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        with Session(get_engine()) as session:
            try:
                _authorize(request, session)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )
                await response(scope, receive, send)
                return
        await self.inner(scope, receive, send)


# The built-in server's editable fields that are part of config_hash: changing one
# restarts its bridge.
_RESTARTING_FIELDS = {"mcp_http", "disabled_tools", "tool_overrides", "normalize_schema_dialect"}


def _refuse_self(server_id: str, what: str) -> None:
    """Refuse an operation that would stop or restart the built-in server's bridge when
    the call is travelling through that same bridge: the reconciler can stop it before
    it relays the result, so the caller sees a transport error for a change that
    landed. The dashboard and /api (or /api/mcp directly) still do it."""
    if not _internal(get_http_request()):
        return
    with Session(get_engine()) as session:
        builtin = service.get_builtin(session)
    if builtin is not None and builtin.id == server_id:
        raise ToolError(
            f"the built-in server can't {what} itself through its own connection; "
            "use the dashboard or the /api route"
        )




def _refusal(request: Request, cloudflare: bool) -> HTTPException:
    """Why the request's credential can't drive the control plane, in words an
    operator can act on. 401 only when nothing was presented: a 401 sends OAuth
    clients back through sign-in, which can't fix a credential that names no user."""
    if request.headers.get(cf_access.HEADER):
        if not cloudflare:
            why = ("a Cloudflare Access identity counts only through the built-in "
                   "server's /s URL, not /api/mcp directly")
        else:
            # Verifying again is fine: this is the failure path, and keys are cached.
            address, why = cf_access.verify(request)
            if address is not None:
                why = (f"Cloudflare Access signed you in as {address}, but no single user "
                       "has that name (none, or more than one)")
        return HTTPException(status_code=403, detail=why)
    if principal_mod._bearer(request):
        return HTTPException(
            status_code=403,
            detail="this server needs a control (login) token or MCPE_ADMIN_TOKEN; "
            "the token sent isn't one",
        )
    return HTTPException(
        status_code=401,
        detail="control-plane auth required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorize(request: Request, session: Session, *, edge: bool = False) -> None:
    """The built-in server's gate. ``edge`` is the /s check in front of its bridge:
    the request hasn't been through the bridge yet, so a Cloudflare Access identity
    is taken directly, and an unusable credential is refused here with a reason
    instead of surfacing as a failed ``initialize``."""
    builtin = service.get_builtin(session)
    if builtin is None or not builtin.enabled:
        raise HTTPException(status_code=404, detail="the mcpelevator MCP server is turned off")
    internal = _internal(request)
    credential = request.headers.get("authorization") or request.headers.get(cf_access.HEADER)
    if not edge and internal and not credential and not request.headers.get(GROUP_HEADER):
        # The supervisor's discovery probe listing tools; calls still authenticate
        # per route. A group request is a client's, so it never counts as one.
        return
    if enforcement_enabled(session):
        cloudflare = edge or internal
        caller = principal_mod.credential_principal(request, session, cloudflare=cloudflare)
        if caller is None:
            raise _refusal(request, cloudflare)
        user_id = caller.user_id
    else:
        # Every caller is the local admin, but a presented credential still names
        # its user, and that user's switch still applies.
        bearer = principal_mod._bearer(request)
        row = repo.get_token_by_hash(session, hash_token(bearer)) if bearer else None
        user_id = row.user_id if row is not None else None
        # A Cloudflare identity names the caller only when no control credential
        # does: an admin or userless token outranks it, as with enforcement on.
        if row is None and not is_env_admin_token(bearer) and (edge or internal):
            signed_in = cf_access.user(session, request)
            user_id = signed_in.id if signed_in is not None else None
    if user_id is not None:
        user = repo.get_user(session, user_id)
        if user is None or not user.control_mcp:
            raise HTTPException(
                status_code=403, detail="the mcpelevator MCP server is disabled for this user"
            )


def authorize_edge(request: Request) -> None:
    """The /s gate for the built-in server (see ``_authorize``)."""
    with Session(get_engine()) as session:
        _authorize(request, session, edge=True)

