"""mcpelevator runner: the built-in control-plane server (``app.control_mcp``).

The upstream is this very process's ``/api/mcp``, fronted by an ordinary remote
bridge, so the server gets every per-server feature (tool hiding/renaming, REST,
auth provider, idle, usage, logs) for free. Nothing about the upstream is stored on
the row: the URL is derived from the bind settings at spec-build time, and the bridge
authenticates its own hop with a per-process secret header. The CALLER's
``Authorization`` is forwarded through the bridge untouched, so ``/api/mcp`` still
authorizes every tool call as that caller.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable

from app.config import get_settings
from app.db.models import BUILTIN_RUNNER, Server
from app.runners.base import ProcessSpec, register

PATH = "/api/mcp"
INTERNAL_HEADER = "x-mcpe-internal"
# The Host the caller reached /s on (already Host-guard validated there). The proxy
# sets it for this server only and /api/mcp trusts it only next to INTERNAL_HEADER,
# so the API's copyable URLs and OAuth callback name the caller's address.
ORIGINAL_HOST_HEADER = "x-mcpe-original-host"
# Set by the group route on every request it hands a bundle: inside a group this
# server's tools carry its slug as a prefix, so prompts must name them that way.
GROUP_HEADER = "x-mcpe-group"
# Proves a request came through this process's own bridge (which only the /s gate, a
# group bundle, or the supervisor's discovery probe can reach). Never persisted:
# bridges restart on boot.
INTERNAL_SECRET = secrets.token_urlsafe(32)


def is_internal(request) -> bool:
    """Did this request come through the built-in server's own bridge?"""
    # Bytes: compare_digest raises on a non-ASCII str, which a client can send.
    return secrets.compare_digest(
        request.headers.get(INTERNAL_HEADER, "").encode(), INTERNAL_SECRET.encode()
    )


# Served by /api/mcp directly and handed to the bridge, which would otherwise
# answer initialize with no instructions of its own.
INSTRUCTIONS = """\
Manage this mcpelevator instance: the MCP servers it hosts (each served at
/s/<slug>/mcp), groups bundling them (/g/<name>/mcp), access tokens, users,
runtime settings, usage counters, and the public MCP catalog. whoami says who
this connection acts as; every call runs with that caller's permissions.

Typical flows:
- Install from the catalog: search_catalog, then install_from_catalog with the
  entry's id (pass env for required values), then wait_for_server. A "failed"
  state is explained by get_server_logs. For full control, get_catalog_server
  returns the launch spec to edit and pass to create_server.
- Install from a GitHub repository (a server not published to npm or PyPI):
  install_from_repository with the repo URL clones it, runs the detected install/
  build steps, and launches it from the checkout (override post_clone_script,
  command, args). get_repository_draft returns the draft to edit by hand; fix a
  build later with update_server's post_clone_script.
- Serve a REST API from its OpenAPI document: create_server with runner
  "openapi", command the document's http(s) URL (or command "" and the
  document's JSON/YAML text in openapi_document), args [] (or [base_url] to
  override its servers entry), env the API's HTTP headers, and enabled true;
  then wait_for_server. Each operation becomes a tool. A failed start is
  explained by one "openapi: ..." line in get_server_logs. restart_server
  re-reads the document.
- Bulk add: import_servers with a standard {"mcpServers": {...}} config. Imported
  servers start disabled. Review them, then enable_server and wait_for_server.
- Hide or relabel a server's tools: update_server with disabled_tools or
  tool_overrides.
- Try a tool: call_server_tool on a running server (not counted as usage).
- Reach a server from a client: mint a token with create_token (scope "all", a
  server id, or "group:<name>"). The plaintext is returned once.
Server ids (not slugs) identify servers in every tool. Lifecycle tools write
desired state and the supervisor converges, so use wait_for_server after them.
Read-only views are also resources (mcpelevator://servers, .../servers/{id},
.../servers/{id}/logs, mcpelevator://groups, mcpelevator://settings), and the
install_server and diagnose_server prompts walk through the common jobs.
"""


def localize(text: str, server: Server | None, prefix: str = "", tools: Iterable[str] = ()) -> str:
    """``text`` as this server's clients see its tools: renamed tools under their new
    names, and hidden ones listed, so guidance never names a tool that isn't there.
    ``prefix`` (a group's ``<slug>_``) goes on each of ``tools`` and every rename."""
    if server is None:
        return text
    renamed = {u: o["name"] for u, o in (server.tool_overrides or {}).items() if o.get("name")}
    names = {u: prefix + n for u, n in renamed.items()}
    if prefix:
        names.update({t: prefix + renamed.get(t, t) for t in tools})
    if names:  # one pass, so a rename is never renamed or prefixed twice
        pattern = r"\b(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")\b"
        text = re.sub(pattern, lambda m: names[m.group(1)], text)
    hidden = sorted(server.disabled_tools or [])
    if hidden:
        text += f"\nHidden on this server, so not callable: {', '.join(hidden)}."
    return text


def internal_url() -> str:
    settings = get_settings()
    host = settings.host
    if host in ("", "0.0.0.0", "localhost"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"  # an IPv6 wildcard may be IPv6-only
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}{PATH}"


@register(BUILTIN_RUNNER)
def build(server: Server) -> ProcessSpec:
    return ProcessSpec(
        command=internal_url(),
        env={INTERNAL_HEADER: INTERNAL_SECRET},  # upstream HTTP headers
        transport="streamable-http",
        disabled_tools=list(server.disabled_tools or []),
        tool_overrides={k: dict(v) for k, v in (server.tool_overrides or {}).items()},
        normalize_schema_dialect=bool(server.normalize_schema_dialect),
        forward_authorization=True,
        instructions=localize(INSTRUCTIONS, server),
    )
