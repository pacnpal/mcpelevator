"""openapi runner — serve a plain REST API as an MCP server, from its OpenAPI document.

There is no MCP upstream at all: the bridge host fetches the OpenAPI document at startup
and serves each operation as an MCP tool (FastMCP's ``OpenAPIProvider``; see
:mod:`app.bridge.openapi`), so every surface downstream of the bridge — tool policy, the
REST mirror, groups, idle, usage — treats it like any other server. The Server row reuses
the launch spec verbatim (SSOT): ``command`` is the document URL, ``args`` is ``[]`` or
``[base_url]`` (an override for the API's own ``servers`` entry), and ``env`` is the HTTP
headers sent to the API. So ``config_hash`` already covers every input, and re-activating
the server (a restart) re-reads the document — the way a restart re-discovers an MCP
upstream's tools. Like every runner this is a pure ``Server -> ProcessSpec`` mapping.
"""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, register

# The bridge transport discriminator for this runner (see app.bridge.host._build_transport).
TRANSPORT = "openapi"


@register("openapi")
def build(server: Server) -> ProcessSpec:
    return ProcessSpec(
        command=server.command,  # OpenAPI document URL
        args=list(server.args or []),  # [] or [base_url]
        env=dict(server.env or {}),  # HTTP headers for the API
        transport=TRANSPORT,
        disabled_tools=list(server.disabled_tools or []),
        tool_overrides={k: dict(v) for k, v in (server.tool_overrides or {}).items()},
        normalize_schema_dialect=bool(server.normalize_schema_dialect),
    )
