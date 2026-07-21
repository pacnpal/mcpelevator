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


def canonical_transport(value: object) -> str | None:
    """Map a transport name/alias to its canonical form, or ``None`` if unsupported.

    A missing/empty value defaults to ``streamable-http`` (the common case). A truthy
    non-string (e.g. a number from a malformed registry record) is coerced to its string
    form, which simply won't match an alias → ``None`` (unsupported) rather than raising.
    Used as the SSOT gate everywhere a remote transport is validated or filtered.
    """
    text = str(value).strip().lower() if value else DEFAULT_TRANSPORT
    return TRANSPORT_ALIASES.get(text)


@register("remote")
def build(server: Server) -> ProcessSpec:
    args = server.args or []
    # Rows are stored canonical (service.normalize_remote), but canonicalize again so
    # a hand-written/legacy row still yields a transport the bridge can build.
    transport = canonical_transport(args[0] if args else None) or DEFAULT_TRANSPORT
    oauth = None
    if server.oauth:
        # The bridge builds a refresh-only OAuth auth from this. It reads the tokens AND
        # the DCR/static client info (including any secret) from the shared file store
        # keyed by server id — the control-plane flow persists them there — so the static
        # client id/secret are intentionally NOT carried in the spec (which is serialized
        # into the child's environment): a credential shouldn't ride along where it isn't used.
        oauth = {
            "server_id": server.id,
            "url": server.command,
            "scopes": server.oauth_scopes or "",
        }
    return ProcessSpec(
        command=server.command,  # upstream URL
        env=dict(server.env or {}),  # upstream HTTP headers
        transport=transport,
        oauth=oauth,
        disabled_tools=list(server.disabled_tools or []),
    )
