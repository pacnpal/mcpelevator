"""A minimal raw JSON-RPC stdio MCP server that, like the official Go SDK, rejects a
second ``initialize`` on the same connection with ``duplicate "initialize" received``,
and refuses any other request sent before the handshake.

The Python/TS SDKs tolerate a re-initialize, so they can't catch a bridge that
re-handshakes a kept-alive stdio child; this fixture can."""

from __future__ import annotations

import json
import sys

initialized = False

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue  # notifications (notifications/initialized, cancellations, ...)
    method, rid = msg.get("method"), msg["id"]
    if method == "initialize":
        if initialized:
            reply = {"code": -32600, "message": 'duplicate "initialize" received'}
            out = {"jsonrpc": "2.0", "id": rid, "error": reply}
        else:
            initialized = True
            out = {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "strict", "version": "0"},
                },
            }
    elif not initialized:
        out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "not initialized"}}
    elif method == "tools/list":
        tool = {"name": "ping", "inputSchema": {"type": "object"}}
        out = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [tool]}}
    elif method == "tools/call":
        content = [{"type": "text", "text": "pong"}]
        out = {"jsonrpc": "2.0", "id": rid, "result": {"content": content}}
    elif method == "ping":
        out = {"jsonrpc": "2.0", "id": rid, "result": {}}
    else:
        out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
