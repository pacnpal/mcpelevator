"""npx runner — Node-based MCP servers (``npx -y <pkg> …``)."""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, passthrough, register


@register("npx")
def build(server: Server) -> ProcessSpec:
    return passthrough(server)
