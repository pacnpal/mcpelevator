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


def pin_insert_index(command: str, args: list[str]) -> Optional[int]:
    """Where the pin belongs in ``args``, or ``None`` when placement is not certain.

    ``uvx`` takes ``--with`` as a leading option. This runner also covers imported
    configs launched via ``uv`` (``registry.service._infer_runner``), where
    ``--with`` is only valid AFTER the run subcommand — so the pin is injected
    only when that subcommand LEADS the args (``uv tool run …`` / ``uv run …``),
    the shapes real imports take. Anything else has no certain placement: with
    leading global options, telling an option operand from the subcommand (both
    can be the bare word ``run``) requires uv's full option grammar, and scanning
    deeper can land the pin inside a child command's own argv. The service
    REFUSES to enable the pin for such shapes (``registry.service``), so the
    operator learns the limitation at save time; the ``None``-guard in ``build``
    backstops any legacy row that predates that check — no pin at worst leaves
    the original failure (and its hint) in place, while a wrong pin breaks or
    silently misdirects the launch."""
    base = command.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base in ("uvx", "uvx.exe"):
        return 0
    if base in ("uv", "uv.exe"):
        if args[:2] == ["tool", "run"]:
            return 2
        if args[:1] == ["run"]:
            return 1
        return None
    # An uvx-classified row can carry any executable (Advanced raw config, API);
    # only the uv/uvx launchers are known to accept --with at all.
    return None


@register("uvx")
def build(server: Server) -> ProcessSpec:
    spec = passthrough(server)
    if server.pin_mcp1:
        # Injected here, not into the stored args: the row keeps exactly what the
        # operator wrote, so the UI's friendly fields (args[0] = package) round-trip.
        at = pin_insert_index(spec.command, spec.args)
        if at is not None:
            spec = replace(spec, args=[*spec.args[:at], *PIN_MCP1_ARGS, *spec.args[at:]])
    return spec
