"""graphql runner — serve a GraphQL API as an MCP server, from its schema.

Like the openapi runner there is no MCP upstream: the bridge host loads the API's schema
at startup and serves each query and mutation field as an MCP tool (see
:mod:`app.bridge.graphql`), so every surface downstream of the bridge — tool policy, the
REST mirror, groups, idle, usage — treats it like any other server. The Server row reuses
the launch spec verbatim (SSOT): ``command`` is the GraphQL endpoint, ``args`` is ``[]``
(introspect the endpoint) or ``[schema_url]`` (an SDL or introspection-JSON file
published elsewhere, for an API with introspection off), and ``env`` is the HTTP headers
sent to the API. A schema pasted or uploaded instead lives in ``openapi_document`` (the
stored API document, shared with the openapi runner) and reaches the bridge on stdin. So
``config_hash`` covers every input, and a restart re-reads the schema. Like every runner
this is a pure ``Server -> ProcessSpec`` mapping.
"""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, register

# The bridge transport discriminator for this runner (see app.bridge.host.build_proxy).
TRANSPORT = "graphql"


@register("graphql")
def build(server: Server) -> ProcessSpec:
    return ProcessSpec(
        command=server.command,  # GraphQL endpoint URL
        args=list(server.args or []),  # [] or [schema_url]
        env=dict(server.env or {}),  # HTTP headers for the API
        transport=TRANSPORT,
        disabled_tools=list(server.disabled_tools or []),
        tool_overrides={k: dict(v) for k, v in (server.tool_overrides or {}).items()},
        normalize_schema_dialect=bool(server.normalize_schema_dialect),
        openapi_document=server.openapi_document or "",  # pasted/uploaded schema
    )
