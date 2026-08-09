"""uvx runner — Python-based MCP servers (``uvx <tool> …``)."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from app.db.models import Server
from app.runners.base import ProcessSpec, passthrough, register

# The compatibility pin behind ``Server.pin_mcp1``: uvx resolves the package's own
# (often unbounded) ``mcp>=…`` constraint fresh on every cold start, so a server
# that predates the SDK's 2.x line dies at import until this holds it to 1.x.
PIN_MCP1_ARGS = ["--with", "mcp<2"]


def _pin_insert_index(command: str, args: list[str]) -> Optional[int]:
    """Where the pin belongs in ``args``, or ``None`` when there is no valid spot.

    ``uvx`` takes ``--with`` as a leading option, but this runner also covers
    imported configs launched via ``uv`` (``registry.service._infer_runner``),
    where ``--with`` is only valid AFTER the subcommand — and ``uv`` accepts
    global options (e.g. ``--directory``) BEFORE it, so the subcommand is located
    rather than assumed to lead: ``uv [OPTIONS] tool run --with …`` /
    ``uv [OPTIONS] run --with …``. A ``uv`` invocation with no run subcommand has
    no place the pin is valid (and isn't running a package anyway) — skip it
    rather than corrupt the argv."""
    base = command.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base not in ("uv", "uv.exe"):
        return 0
    for i, arg in enumerate(args):
        if arg == "tool" and i + 1 < len(args) and args[i + 1] == "run":
            return i + 2
        if arg == "run":
            return i + 1
    return None


@register("uvx")
def build(server: Server) -> ProcessSpec:
    spec = passthrough(server)
    if server.pin_mcp1:
        # Injected here, not into the stored args: the row keeps exactly what the
        # operator wrote, so the UI's friendly fields (args[0] = package) round-trip.
        at = _pin_insert_index(spec.command, spec.args)
        if at is not None:
            spec = replace(spec, args=[*spec.args[:at], *PIN_MCP1_ARGS, *spec.args[at:]])
    return spec
