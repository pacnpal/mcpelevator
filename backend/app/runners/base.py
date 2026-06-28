"""Runner seam — turns a Server row into a literal process spec.

A Runner is a pure function ``Server -> ProcessSpec`` (no I/O, no globals): same
row always yields the same argv (Determinism). The bridge host then launches that
argv as a stdio MCP server via FastMCP's ``StdioTransport``. Adding a new runner
type is one small module that registers a builder — callers never change.

We store ``command``/``args`` verbatim in the mcpServers-compatible shape, so the
default builder is near-passthrough; per-runner modules add only what differs
(e.g. docker injects hardening flags).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.db.models import Server


@dataclass(frozen=True)
class ProcessSpec:
    """What the bridge host will front.

    For ``transport == "stdio"`` (the default, every local runner) this is the
    literal stdio command to launch: ``command``/``args``/``env``/``cwd``. For a
    remote runner the same fields are reused with different meaning — ``command``
    is the upstream URL, ``env`` is the upstream HTTP headers, and ``transport``
    selects the remote client (``streamable-http`` | ``sse``). The discriminator
    keeps the runner seam a pure ``Server -> ProcessSpec`` mapping either way.
    """

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # server-specific vars / headers
    cwd: str | None = None
    transport: str = "stdio"  # stdio | streamable-http | sse


Builder = Callable[[Server], ProcessSpec]
_BUILDERS: dict[str, Builder] = {}


def register(runner: str) -> Callable[[Builder], Builder]:
    def deco(fn: Builder) -> Builder:
        _BUILDERS[runner] = fn
        return fn
    return deco


def build_spec(server: Server) -> ProcessSpec:
    builder = _BUILDERS.get(server.runner)
    if builder is None:
        raise ValueError(f"no runner builder registered for {server.runner!r}")
    return builder(server)


def passthrough(server: Server) -> ProcessSpec:
    """Verbatim command/args — the shared default for npx/uvx/command."""
    return ProcessSpec(
        command=server.command,
        args=list(server.args or []),
        env=dict(server.env or {}),
        cwd=server.cwd,
    )
