"""command runner — an arbitrary local executable that speaks MCP over stdio."""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, passthrough, register


@register("command")
def build(server: Server) -> ProcessSpec:
    return passthrough(server)
