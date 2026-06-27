"""remote runner — proxy an already-remote MCP server (Streamable-HTTP / SSE).

Unlike the local runners, there is no process to spawn: the bridge host fronts a
remote upstream transport instead of a stdio one. The Server row reuses the launch
spec verbatim (SSOT) — ``command`` is the upstream URL, ``args[0]`` is the
transport, ``env`` is the upstream HTTP headers — so ``config_hash`` already covers
every input and a change re-hashes into exactly one idempotent reconcile. Like every
runner this is a pure ``Server -> ProcessSpec`` mapping (Determinism).
"""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, register

# The remote client transports the bridge host knows how to build.
TRANSPORTS = ("streamable-http", "sse")
DEFAULT_TRANSPORT = "streamable-http"


@register("remote")
def build(server: Server) -> ProcessSpec:
    args = server.args or []
    transport = str(args[0]) if args else DEFAULT_TRANSPORT
    return ProcessSpec(
        command=server.command,  # upstream URL
        env=dict(server.env or {}),  # upstream HTTP headers
        transport=transport,
    )
