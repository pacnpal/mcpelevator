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

# The remote client transports the bridge host knows how to build. This module is
# the single source of truth for the remote transport vocabulary — the registry
# service (validation) and the catalog (installability) both canonicalize through
# `canonical_transport` so there is one place that decides what "remote" supports.
TRANSPORTS = ("streamable-http", "sse")
DEFAULT_TRANSPORT = "streamable-http"
TRANSPORT_ALIASES = {
    "http": "streamable-http",
    "streamable-http": "streamable-http",
    "streamable_http": "streamable-http",
    "streamablehttp": "streamable-http",
    "sse": "sse",
}


def canonical_transport(value: str | None) -> str | None:
    """Map a transport name/alias to its canonical form, or ``None`` if unsupported.

    A missing/empty value defaults to ``streamable-http`` (the common case). Used as
    the SSOT gate everywhere a remote transport is validated or filtered.
    """
    return TRANSPORT_ALIASES.get((value or DEFAULT_TRANSPORT).strip().lower())


@register("remote")
def build(server: Server) -> ProcessSpec:
    args = server.args or []
    # Rows are stored canonical (service.normalize_remote), but canonicalize again so
    # a hand-written/legacy row still yields a transport the bridge can build.
    transport = canonical_transport(args[0] if args else None) or DEFAULT_TRANSPORT
    return ProcessSpec(
        command=server.command,  # upstream URL
        env=dict(server.env or {}),  # upstream HTTP headers
        transport=transport,
    )
