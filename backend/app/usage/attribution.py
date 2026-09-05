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

# Bodies above this are not parsed for a tool name. A JSON-RPC envelope is small;
# something this large is a payload (an inlined image, a pasted file), and parsing
# it would cost more than the count is worth. Such a request still counts as
# traffic, just not against a tool.
MAX_PARSE_BYTES = 1 << 20  # 1 MiB

# The tool name is CLIENT-controlled — a caller may name a tool that was never
# advertised — so what gets past this function bounds what the counters can hold.
# A real MCP tool name is a function identifier; anything longer is not one, and
# storing it would let a caller choose the size of a stored row.
MAX_TOOL_NAME = 128
# There is deliberately NO length bound at parse time. A group call names its tool
# qualified by the member's slug (`<slug>_<tool>`) and slugs have no length limit,
# so ANY fixed cap here eventually drops a call the hub really serves — a longer
# cap just moves the slug length at which it happens. What bounds this is already
# in place and does not depend on guessing: the body is capped by MAX_PARSE_BYTES,
# the element count by MAX_BATCH_NAMES, and the STORED name by `recorder.record`,
# which applies MAX_TOOL_NAME after `split_namespaced` has taken the prefix off.

# A batch is counted per element, so cap how many elements one body may contribute.
MAX_BATCH_NAMES = 64

_REST_PREFIX = "rest/"


def tools_from_body(body: bytes) -> list[str]:
    """The tool names a JSON-RPC request invokes — empty when it invokes none.

    Tolerates a batch array (a list of envelopes) even though MCP 2025-06-18
    dropped batching: an older client may still send one, and counting each of
    its TOOL CALLS beats counting the batch as a single call — bounded by
    :data:`MAX_BATCH_NAMES` so one request can't mint an unbounded number of
    counter keys.

    Per-element accounting stops at tool calls. A batch's non-tool elements
    (``initialize``, ``tools/list``) are not counted individually: the whole
    request still contributes exactly one plain-traffic count when it names no
    tool at all, and none when it names one. Splitting them out would mean
    `record` accepting a count of non-tool entries alongside the names, which
    buys a more precise `other_requests` for a message shape the current protocol
    no longer defines — not worth the widened contract.

    A body that isn't JSON, isn't an envelope, is oversized, or is nested deeply
    enough to exhaust the parser's stack yields no names rather than raising:
    usage accounting must never be able to fail a request it only observes, and
    ``json.loads`` raises ``RecursionError`` — not a ``ValueError`` — on a
    deeply nested payload that is still well under the size cap."""
    if not body or len(body) > MAX_PARSE_BYTES:
        return []
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return []
    entries = payload if isinstance(payload, list) else [payload]
    names: list[str] = []
    for entry in entries:
        if len(names) >= MAX_BATCH_NAMES:
            break
        if not isinstance(entry, dict) or entry.get("method") != "tools/call":
            continue
        params = entry.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def proxy_tools(method: str, path: str, body: bytes, *, rest_enabled: bool = True) -> list[str]:
    """The tools a ``/s/<slug>/<path>`` request invoked.

    Two exposed surfaces reach the same tools, so both are attributed here:
    the MCP endpoint (``mcp``, tool named in the JSON-RPC body) and the REST
    mirror (``POST rest/<tool>``, tool named in the path).

    ``rest_enabled`` is the server's own ``rest_openapi`` exposure. With it off —
    the DEFAULT — the bridge installs no ``/rest/*`` routes at all, so such a
    request only ever collects a 404; counting it would let traffic aimed at a
    surface the server does not serve invent tool rows and inflate `tool_calls`.

    The MCP path is matched EXACTLY, not slash-stripped. The bridge registers
    ``/mcp`` alone, so ``/mcp/`` is a 307 back to it — no tool is invoked. A
    lenient match counts that redirect, then counts the followed request too, so
    one call lands twice; a client that ignores the redirect gets counted for a
    request nothing served. Neither variant is a tool call, and both still count
    as plain traffic like any other non-tool request.

    Both surfaces require POST, because only POST dispatches a call: on the MCP
    endpoint a GET opens the event stream and a DELETE ends the session, and the
    remaining methods are refused outright. A `tools/call`-shaped body sent with
    any of them invokes nothing, so attributing it would let a caller inflate a
    real tool's counter without ever running it.

    The path is compared EXACTLY as the proxy forwards it — not stripped of
    slashes. `/s/<slug>//mcp` arrives here as `/mcp` and is relayed upstream as
    `//mcp`, which the bridge does not serve; normalizing it away would count a
    request that only ever got an error back. Same for a doubled slash before
    `rest/<tool>`. A request like that still counts as plain traffic, as any
    other non-tool request does."""
    if method.upper() != "POST":
        return []
    normalized = path
    if normalized == "mcp":
        return tools_from_body(body)
    if rest_enabled and normalized.startswith(_REST_PREFIX):
        tool = normalized[len(_REST_PREFIX):]
        # `openapi.json` is NOT excluded here. The generated document is served on
        # `GET /rest/openapi.json` and this branch is POST-only, so a POST to that
        # path reaches the dynamic `/rest/{tool}` route like any other; excluding
        # the name would silently mis-file a server that genuinely exposes a tool
        # called `openapi.json` as plain traffic.
        if tool and "/" not in tool:
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
