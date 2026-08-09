"""uvx runner — Python-based MCP servers (``uvx <tool> …``)."""

from __future__ import annotations

from dataclasses import replace

from app.db.models import Server
from app.runners.base import ProcessSpec, passthrough, register

# The compatibility pin behind ``Server.pin_mcp1``: uvx resolves the package's own
# (often unbounded) ``mcp>=…`` constraint fresh on every cold start, so a server
# that predates the SDK's 2.x line dies at import until this holds it to 1.x.
PIN_MCP1_ARGS = ["--with", "mcp<2"]


def _pin_insert_index(command: str, args: list[str]) -> int:
    """Where the pin belongs in ``args``. ``uvx`` takes ``--with`` as a leading
    option, but this runner also covers imported configs launched via ``uv``
    (``registry.service._infer_runner``), where ``--with`` is only valid AFTER the
    subcommand words — ``uv tool run --with …`` / ``uv run --with …`` — and a
    leading ``--with`` is a launcher parse error."""
    base = command.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base not in ("uv", "uv.exe"):
        return 0
    i = 0
    if i < len(args) and args[i] == "tool":
        i += 1
    if i < len(args) and args[i] == "run":
        i += 1
    return i


@register("uvx")
def build(server: Server) -> ProcessSpec:
    spec = passthrough(server)
    if server.pin_mcp1:
        # Injected here, not into the stored args: the row keeps exactly what the
        # operator wrote, so the UI's friendly fields (args[0] = package) round-trip.
        at = _pin_insert_index(spec.command, spec.args)
        spec = replace(spec, args=[*spec.args[:at], *PIN_MCP1_ARGS, *spec.args[at:]])
    return spec
