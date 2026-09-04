"""Which tool — if any — a data-plane request invoked.

Pure functions over what the caller already holds (the path under ``/s/<slug>/``
or ``/g/<name>/`` and the request body it already buffered): no I/O, no request
object, no DB. Both recording sites — the per-server proxy and the group
dispatcher — share this ONE rulebook, so "a tool call" means the same thing on
every exposed surface, and the rules stay unit-testable without a bridge.

Only names are read. Tool arguments are never inspected or stored.
"""

from __future__ import annotations

import json
from typing import Iterable

# Traffic that isn't a tool call at all (initialize, tools/list, the SSE GET) is
# counted under this sentinel tool name — see db.models.UsageBucket.
NOT_A_TOOL = ""

# Bodies above this are not parsed for a tool name. A JSON-RPC envelope is small;
# something this large is a payload (an inlined image, a pasted file), and parsing
# it would cost more than the count is worth. Such a request still counts as
# traffic, just not against a tool.
MAX_PARSE_BYTES = 1 << 20  # 1 MiB

_REST_PREFIX = "rest/"
# The REST surface's own non-tool routes (bridge.host.build_rest_routes).
_REST_NON_TOOL = {"openapi.json"}


def tools_from_body(body: bytes) -> list[str]:
    """The tool names a JSON-RPC request invokes — empty when it invokes none.

    Tolerates a batch array (a list of envelopes) even though MCP 2025-06-18
    dropped batching: an older client may still send one, and counting each
    element beats counting the batch as a single call. A body that isn't JSON,
    isn't an envelope, or is oversized yields no names rather than raising —
    usage accounting must never be able to fail a request."""
    if not body or len(body) > MAX_PARSE_BYTES:
        return []
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return []
    entries = payload if isinstance(payload, list) else [payload]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("method") != "tools/call":
            continue
        params = entry.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def proxy_tools(method: str, path: str, body: bytes) -> list[str]:
    """The tools a ``/s/<slug>/<path>`` request invoked.

    Two exposed surfaces reach the same tools, so both are attributed here:
    the MCP endpoint (``mcp``, tool named in the JSON-RPC body) and the REST
    mirror (``POST rest/<tool>``, tool named in the path)."""
    normalized = path.strip("/")
    if normalized == "mcp":
        return tools_from_body(body)
    if method.upper() == "POST" and normalized.startswith(_REST_PREFIX):
        tool = normalized[len(_REST_PREFIX):]
        if tool and "/" not in tool and tool not in _REST_NON_TOOL:
            return [tool]
    return []


def split_namespaced(name: str, slugs: Iterable[str]) -> tuple[str, str] | None:
    """Split a group-hub tool name (``<slug>_<tool>``) into ``(slug, tool)``.

    The hub namespaces every member's tools by slug, so a group call names the
    member it belongs to — but both slugs and tool names may contain ``_``, so
    the split is resolved against the group's ACTUAL members, longest slug
    first (the only reading that can't mis-attribute a call to a member whose
    slug is a prefix of another's). Returns None when no member matches, which
    is how a call to a tool the hub no longer mounts is dropped instead of
    guessed at."""
    for slug in sorted(slugs, key=len, reverse=True):
        prefix = f"{slug}_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return slug, name[len(prefix):]
    return None
