"""uvx runner — Python-based MCP servers (``uvx <tool> …``)."""

from __future__ import annotations

from dataclasses import replace

from app.db.models import Server
from app.runners.base import ProcessSpec, passthrough, register

# The compatibility pin behind ``Server.pin_mcp1``: uvx resolves the package's own
# (often unbounded) ``mcp>=…`` constraint fresh on every cold start, so a server
# that predates the SDK's 2.x line dies at import until this holds it to 1.x.
PIN_MCP1_ARGS = ["--with", "mcp<2"]


@register("uvx")
def build(server: Server) -> ProcessSpec:
    spec = passthrough(server)
    if server.pin_mcp1:
        # Injected here, not into the stored args: the row keeps exactly what the
        # operator wrote, so the UI's friendly fields (args[0] = package) round-trip.
        spec = replace(spec, args=[*PIN_MCP1_ARGS, *spec.args])
    return spec
