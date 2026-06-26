"""uvx runner — Python-based MCP servers (``uvx <tool> …``)."""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, passthrough, register


@register("uvx")
def build(server: Server) -> ProcessSpec:
    return passthrough(server)
