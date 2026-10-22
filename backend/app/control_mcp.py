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

The endpoint itself is gated like the sensitive routers (``require_control_plane``)
before the MCP app sees the request, so an unauthenticated client can't even list
tools once enforcement is on. The exception is the built-in bridge (internal header,
no Authorization), which is how the supervisor's discovery probe lists the tools;
every tool call still needs the caller's own credential. It runs stateless.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Optional

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
from app.auth.control_plane import require_control_plane
from app.db import get_engine, repo
from app.registry import service
from app.runners.builtin import INTERNAL_HEADER, INTERNAL_SECRET, PATH

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

_READ = {"readOnlyHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

INSTRUCTIONS = """\
Manage this mcpelevator instance: the MCP servers it hosts (each served at
/s/<slug>/mcp), groups bundling them (/g/<name>/mcp), access tokens, users,
runtime settings, usage counters, and the public MCP catalog.

Typical flows:
- Install from the catalog: search_catalog -> get_catalog_server (its launch spec)
  -> create_server with that spec -> enable_server -> get_server until state is
  "running" (get_server_logs explains a "failed" state).
- Bulk add: import_servers with a standard {"mcpServers": {...}} config; imported
  servers start disabled, review then enable_server.
- Try a tool: call_server_tool on a running server (not counted as usage).
- Reach a server from a client: mint a token with create_token (scope "all", a
  server id, or "group:<name>"); the plaintext is returned once.
Server ids (not slugs) identify servers in every tool. Lifecycle tools write
desired state; the supervisor converges, so state may lag a moment.
"""


def build(app: FastAPI) -> "_Gate":
    """The gated ``/api/mcp`` ASGI app for ``app``. The inner MCP app (``.inner``) has
    a lifespan the host app must run (see ``main.lifespan``)."""
    mcp = FastMCP("mcpelevator", instructions=INSTRUCTIONS, version=__version__)

    async def api(method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        outer = get_http_request()
        headers = {k: v for k, v in outer.headers.items() if k not in _TRANSPORT_HEADERS}
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
        if not r.content:
            return {"ok": True}
        data = r.json()
        # A bare list would reach the client as one content block per item (none when
        # empty); one JSON object is what every tool result should be.
        return {"items": data} if isinstance(data, list) else data

    # ---- Instance -----------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def health_summary() -> Any:
        """Instance health: version and a per-state count of servers."""
        return await api("GET", "/api/health/summary")

    @mcp.tool(annotations=_READ)
    async def server_health(slug: str) -> Any:
        """Health of one server by slug (its live state and readiness)."""
        return await api("GET", f"/api/health/{slug}")

    @mcp.tool(annotations=_READ)
    async def get_settings() -> Any:
        """Runtime settings: bind mode, allowed hosts, default auth provider,
        control-plane auth, idle timeout, usage retention, OAuth, docker runner."""
        return await api("GET", "/api/settings")

    @mcp.tool(annotations=_WRITE)
    async def update_settings(changes: SettingsUpdate) -> Any:
        """Change runtime settings (admin only). Only the fields given change. A change
        that would turn on control-plane auth without an admin credential is refused."""
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
        return await api("GET", f"/api/servers/{server_id}")

    @mcp.tool(annotations=_WRITE)
    async def create_server(spec: ServerCreate) -> Any:
        """Register a server. ``runner`` is one of npx, uvx, command, remote (``command``
        is then the upstream MCP URL), or docker (``command`` is the image). Created
        disabled unless ``enabled`` is true."""
        return await api("POST", "/api/servers", json=spec.model_dump(exclude_unset=True))

    @mcp.tool(annotations=_WRITE)
    async def update_server(server_id: str, changes: ServerUpdate) -> Any:
        """Edit a server; only the fields given change (an explicit null resets the
        nullable ones to inherit). A launch-spec change restarts its bridge."""
        return await api(
            "PATCH", f"/api/servers/{server_id}", json=changes.model_dump(exclude_unset=True)
        )

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_server(server_id: str) -> Any:
        """Stop and permanently delete a server, its tokens, and its group memberships."""
        return await api("DELETE", f"/api/servers/{server_id}")

    @mcp.tool(annotations=_WRITE)
    async def clone_server(server_id: str, name: Optional[str] = None) -> Any:
        """Copy a server's configuration into a new, disabled server."""
        return await api("POST", f"/api/servers/{server_id}/clone", json={"name": name})

    @mcp.tool(annotations=_WRITE)
    async def import_servers(config: dict) -> Any:
        """Bulk-create from a standard ``{"mcpServers": {...}}`` config (as used by
        Claude Desktop and others). Imported servers start disabled."""
        return await api("POST", "/api/servers/import", json=config)

    @mcp.tool(annotations=_WRITE)
    async def enable_server(server_id: str) -> Any:
        """Set a server desired-running; the supervisor starts it."""
        return await api("POST", f"/api/servers/{server_id}/enable")

    @mcp.tool(annotations=_WRITE)
    async def disable_server(server_id: str) -> Any:
        """Set a server desired-stopped; the supervisor stops it."""
        return await api("POST", f"/api/servers/{server_id}/disable")

    @mcp.tool(annotations=_WRITE)
    async def retry_server(server_id: str) -> Any:
        """Start a fresh activation of a failed server."""
        return await api("POST", f"/api/servers/{server_id}/retry")

    @mcp.tool(annotations=_WRITE)
    async def restart_server(server_id: str) -> Any:
        """Restart an enabled server's bridge from unchanged config (re-discovers tools)."""
        if get_http_request().headers.get(INTERNAL_HEADER):
            with Session(get_engine()) as session:
                builtin = service.get_builtin(session)
            if builtin is not None and builtin.id == server_id:
                # Restarting stops the bridge carrying this very call, which then
                # can't deliver the result. Use the dashboard or POST /api/.../restart.
                raise ToolError("the built-in server can't restart itself from here")
        return await api("POST", f"/api/servers/{server_id}/restart")

    @mcp.tool(annotations={**_WRITE, "openWorldHint": True})
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
            f"/api/servers/{server_id}/tools/{tool_name}/call",
            json={"arguments": arguments or {}, "timeout_s": timeout_s},
        )

    @mcp.tool(annotations=_READ)
    async def get_server_logs(
        server_id: str, tail: Annotated[int, Field(ge=1, le=2000)] = 200
    ) -> Any:
        """The last ``tail`` lines of a server's current activation log (setup script,
        bridge, and upstream stderr). It's the first place to look when it failed."""
        await api("GET", f"/api/servers/{server_id}")  # visibility: 404s like the route
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
        return {"running": True, "state": unit.state, "lines": lines}

    @mcp.tool(annotations=_READ)
    async def get_server_usage(server_id: str, days: Annotated[int, Field(ge=1)] = 7) -> Any:
        """One server's tool-call counts over the trailing ``days``: totals, per tool,
        and a series."""
        return await api("GET", f"/api/servers/{server_id}/usage", params={"days": days})

    @mcp.tool(annotations=_WRITE)
    async def start_server_oauth(server_id: str) -> Any:
        """Begin the upstream OAuth sign-in for a remote server with ``oauth`` on.
        Returns the authorization URL a human must open in a browser to finish it."""
        return await api("POST", f"/api/servers/{server_id}/oauth/authorize")

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def disconnect_server_oauth(server_id: str) -> Any:
        """Forget a server's stored upstream OAuth tokens."""
        return await api("POST", f"/api/servers/{server_id}/oauth/disconnect")

    # ---- Groups (admin) -----------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_groups() -> Any:
        """Every group with its members ("*" or server ids) and its /g/<name>/mcp URL."""
        return await api("GET", "/api/groups")

    @mcp.tool(annotations=_WRITE)
    async def put_group(name: str, members: GroupMembers) -> Any:
        """Create or replace a group: ``members`` is "*" (every server, present and
        future) or an ordered list of server ids. Tools surface as <slug>_<tool>."""
        return await api("PUT", f"/api/groups/{name}", json={"members": members})

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_group(name: str) -> Any:
        """Delete a group and revoke the tokens scoped to it."""
        return await api("DELETE", f"/api/groups/{name}")

    @mcp.tool(annotations=_WRITE)
    async def restart_group(name: str) -> Any:
        """Restart every enabled member of a group; reports restarted/skipped/failed."""
        return await api("POST", f"/api/groups/{name}/restart")

    # ---- Tokens -------------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_tokens() -> Any:
        """Tokens visible to the caller (prefix and scope only, never the secret)."""
        return await api("GET", "/api/tokens")

    @mcp.tool(annotations=_WRITE)
    async def create_token(name: str, scope: str = "all") -> Any:
        """Mint a token. ``scope``: "all" (every bearer-protected server), a server id,
        "group:<name>", or "control" (control plane). The plaintext is returned once."""
        return await api("POST", "/api/tokens", json={"name": name, "scope": scope})

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_token(token_id: str) -> Any:
        """Revoke a token."""
        return await api("DELETE", f"/api/tokens/{token_id}")

    # ---- Users (admin) ------------------------------------------------------

    @mcp.tool(annotations=_READ)
    async def list_users() -> Any:
        """Every user with role, local-runner permission, and owned server/token counts."""
        return await api("GET", "/api/users")

    @mcp.tool(annotations=_WRITE)
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
            "PATCH", f"/api/users/{user_id}", json={k: v for k, v in changes.items() if v is not None}
        )

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def delete_user(user_id: str) -> Any:
        """Delete a user and their tokens (refused while they still own servers)."""
        return await api("DELETE", f"/api/users/{user_id}")

    @mcp.tool(annotations=_WRITE)
    async def create_user_credential(user_id: str) -> Any:
        """Mint a control-plane login token for a user; the plaintext is returned once."""
        return await api("POST", f"/api/users/{user_id}/credentials")

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

    # host_origin_protection off: the /api allowlist middleware already ran.
    inner = mcp.http_app(path=PATH, stateless_http=True, host_origin_protection=False)

    return _Gate(inner)


class _Gate:
    """``require_control_plane`` in front of the MCP app. A class, not a function:
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


def _authorize(request: Request, session: Session) -> None:
    builtin = service.get_builtin(session)
    if builtin is None or not builtin.enabled:
        raise HTTPException(status_code=404, detail="the mcpelevator MCP server is turned off")
    internal = secrets.compare_digest(request.headers.get(INTERNAL_HEADER, ""), INTERNAL_SECRET)
    if internal and not request.headers.get("authorization"):
        return  # the built-in bridge listing tools; calls still authenticate per route
    require_control_plane(request, session)
    principal = principal_mod.resolve(request, session)
    if principal is not None and principal.user_id is not None:
        user = repo.get_user(session, principal.user_id)
        if user is None or not user.control_mcp:
            raise HTTPException(
                status_code=403, detail="the mcpelevator MCP server is disabled for this user"
            )

