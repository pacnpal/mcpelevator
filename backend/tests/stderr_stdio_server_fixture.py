"""A raw JSON-RPC stdio MCP server that writes to stderr around its tool calls.

Drives ``attach_stderr_on_error`` (issue #227): a stdio child's stderr goes to the
bridge's log, and with the flag on a failed call also carries the lines written while it
held the child. Every tool writes ``line`` (when given) to stderr BEFORE it answers:

* ``fail`` {line?, delay?}: answers ``isError`` with "boom" after ``delay`` seconds.
* ``rpc_error`` {line?}: answers with a JSON-RPC error (the proxy raises a ToolError).
* ``ok`` {line?, delay?}: answers successfully with "fine" after ``delay`` seconds.

Calls are answered on timer threads, so overlapping calls really overlap unless the
bridge serializes them.
"""

from __future__ import annotations

import json
import sys
import threading
import time

_write_lock = threading.Lock()


def _send(msg: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _call(rid, name: str, args: dict) -> None:
    line = args.get("line")
    if line:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    time.sleep(float(args.get("delay") or 0))
    if name == "rpc_error":
        _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "exploded"}})
        return
    text, error = ("boom", True) if name == "fail" else ("fine", False)
    result = {"content": [{"type": "text", "text": text}], "isError": error}
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


_TOOLS = [{"name": name, "inputSchema": {"type": "object"}} for name in ("fail", "rpc_error", "ok")]

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    if "method" not in msg or "id" not in msg:
        continue  # replies and notifications
    method, rid = msg["method"], msg["id"]
    if method == "initialize":
        result = {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stderr", "version": "0"},
        }
        _send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": _TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        threading.Thread(
            target=_call,
            args=(rid, params.get("name"), params.get("arguments") or {}),
            daemon=True,
        ).start()
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": rid, "result": {}})
    else:
        _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
