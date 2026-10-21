"""A raw JSON-RPC stdio MCP server that sends requests BACK to its client.

The bridge keeps one stdio child per server and shares its connection across every
front-end client, so a request the child sends (``roots/list``,
``sampling/createMessage``, ``elicitation/create``) has to be routed to the right one
(issue #143). This fixture makes that observable:

* ``ask`` {method, delay?}: optionally waits ``delay`` seconds, then sends ``method`` to
  the client mid-call and returns what came back — ``{"result": ...}`` or
  ``{"error": ...}``. Before asking it emits a log message and, when the call carried a
  progress token, a progress notification.
* ``arm`` {method, delay}: answers at once, then sends ``method`` ``delay`` seconds
  LATER, while no call is in flight (an unsolicited server request).
* ``history``: every server-request outcome so far, in order, tagged with how it was sent.
* ``max_inflight``: the most tool calls this child ever had in flight at once.

Requests are answered out of order (a pending map, plus timer threads for delays), so
concurrent calls really are concurrent unless the bridge serializes them.
"""

from __future__ import annotations

import json
import sys
import threading

_write_lock = threading.Lock()
_state_lock = threading.Lock()
_pending: dict[str, tuple] = {}  # server-request id -> (kind, tool-call id | None)
_history: list[dict] = []
_inflight = 0
_max_inflight = 0
_next_id = 0

_PARAMS = {
    "roots/list": {},
    "sampling/createMessage": {
        "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
        "maxTokens": 8,
    },
    "elicitation/create": {
        "message": "who are you?",
        "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
}


def _send(msg: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _text(rid, text: str) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}})


def _finish_call(rid, text: str) -> None:
    global _inflight
    with _state_lock:
        _inflight -= 1
    _text(rid, text)


def _ask_client(method: str, kind: str, call_id) -> None:
    global _next_id
    with _state_lock:
        _next_id += 1
        sid = f"srv-{_next_id}"
        _pending[sid] = (kind, call_id)
    _send({"jsonrpc": "2.0", "id": sid, "method": method, "params": _PARAMS[method]})


def _on_reply(msg: dict) -> None:
    with _state_lock:
        kind, call_id = _pending.pop(msg["id"])
        outcome = {"result": msg["result"]} if "result" in msg else {"error": msg.get("error")}
        _history.append({"kind": kind, **outcome})
    if call_id is not None:
        _finish_call(call_id, json.dumps(outcome))


def _start_call() -> None:
    global _inflight, _max_inflight
    with _state_lock:
        _inflight += 1
        _max_inflight = max(_max_inflight, _inflight)


def _ask(rid, args: dict, meta: dict) -> None:
    method = args["method"]
    _send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "logger": "fixture", "data": {"msg": f"asking {method}"}},
        }
    )
    token = meta.get("progressToken")
    if token is not None:
        _send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 1, "total": 2},
            }
        )
    _ask_client(method, "solicited", rid)


def _handle_call(rid, params: dict) -> None:
    name = params.get("name")
    args = params.get("arguments") or {}
    meta = params.get("_meta") or {}
    if name == "ask":
        _start_call()
        delay = float(args.get("delay") or 0)
        if delay:
            threading.Timer(delay, _ask, (rid, args, meta)).start()
        else:
            _ask(rid, args, meta)
    elif name == "arm":
        _text(rid, "armed")
        threading.Timer(float(args["delay"]), _ask_client, (args["method"], "unsolicited", None)).start()
    elif name == "history":
        with _state_lock:
            snapshot = list(_history)
        _text(rid, json.dumps(snapshot))
    elif name == "max_inflight":
        with _state_lock:
            peak = _max_inflight
        _text(rid, str(peak))
    else:
        _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"no tool {name}"}})


_TOOLS = [
    {"name": name, "inputSchema": {"type": "object"}}
    for name in ("ask", "arm", "history", "max_inflight")
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "method" not in msg:
        _on_reply(msg)  # the client answering one of OUR requests
        continue
    if "id" not in msg:
        continue  # notifications (initialized, cancelled, ...)
    method, rid = msg["method"], msg["id"]
    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}, "logging": {}},
                    "serverInfo": {"name": "asker", "version": "0"},
                },
            }
        )
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": _TOOLS}})
    elif method == "tools/call":
        _handle_call(rid, msg.get("params") or {})
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": rid, "result": {}})
    else:
        _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
