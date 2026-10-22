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

import secrets

from app.config import get_settings
from app.db.models import BUILTIN_RUNNER, Server
from app.runners.base import ProcessSpec, register

PATH = "/api/mcp"
INTERNAL_HEADER = "x-mcpe-internal"
# Proves a request came through this process's own bridge (which only the /s gate or
# the supervisor's discovery probe can reach). Never persisted: bridges restart on boot.
INTERNAL_SECRET = secrets.token_urlsafe(32)


def internal_url() -> str:
    settings = get_settings()
    host = settings.host
    if host in ("", "0.0.0.0", "::", "localhost"):
        host = "127.0.0.1"
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
    )
