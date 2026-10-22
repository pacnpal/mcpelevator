"""Debug mode (``MCPE_DEBUG=true``): one switch for everything a failure report needs.

When it's on:

- Every ``app.*`` logger, plus fastmcp, the MCP SDK and uvicorn, logs at DEBUG to
  stderr with a timestamp and logger name. Bridges inherit the variable, so each
  server's own log (the one the UI shows) gets the same detail, tracebacks included.
- Every HTTP request to the control plane is traced in one line: method, path, the
  JSON-RPC method (and tool name) for MCP traffic, status, time to first byte, peer,
  and the headers, with credentials reduced to something you can match but not use.
  A 4xx/5xx response also logs its body, so a refusal's reason lands in the log.
- The latest traces and log records are kept in memory and served, with the
  version, effective config, runtime settings and every server's state and log tail,
  at ``GET /api/debug`` (admin only). One call gives a whole picture to paste.

When it's off, none of this runs and ``/api/debug`` 404s. Debug output can hold tool
arguments and results, so leave it off unless you're chasing something.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import platform
import secrets
import sys
import time
from importlib import metadata
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__
from app.config import get_settings

logger = logging.getLogger("app.debug")

TRACES: collections.deque = collections.deque(maxlen=500)
RECORDS: collections.deque = collections.deque(maxlen=2000)

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Chatty below these levels without saying anything a failure report needs.
_QUIET = {"httpcore": logging.WARNING, "hpack": logging.WARNING, "multipart": logging.INFO,
          "aiosqlite": logging.INFO, "watchfiles": logging.WARNING, "asyncio": logging.INFO,
          "sse_starlette": logging.INFO}
_SECRET_WORDS = ("token", "secret", "password", "key", "assertion", "cookie", "authorization")
_BODY_LIMIT = 4096
# The API, server and group endpoints, and OAuth discovery; not the SPA's assets.
_TRACED = ("/api", "/s/", "/g/", "/.well-known", "/mcp", "/rest")


def enabled() -> bool:
    return get_settings().debug


class _Ring(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECORDS.append(self.format(record))
        except Exception:  # a log handler must never raise into the caller
            pass


def configure_logging(*, keep: bool) -> None:
    """Turn on DEBUG logging for this process. ``keep`` also records into the ring
    that ``/api/debug`` serves (the control plane; a bridge has no such route)."""
    root = logging.getLogger()
    if any(isinstance(h, _Ring) or getattr(h, "_mcpe_debug", False) for h in root.handlers):
        return  # already on (a second app in the same process, e.g. tests)
    root.setLevel(logging.DEBUG)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if keep:
        handlers.append(_Ring())
    for handler in handlers:
        handler._mcpe_debug = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)
    for name in ("app", "mcpelevator", "fastmcp", "mcp", "uvicorn", "uvicorn.access", "httpx"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    # fastmcp installs its own non-propagating handler; send it through ours instead.
    fastmcp_log = logging.getLogger("fastmcp")
    fastmcp_log.handlers.clear()
    fastmcp_log.propagate = True


# ---- credential fingerprints ---------------------------------------------------


def _jwt_claims(token: str) -> dict | None:
    """A JWT's payload, unverified: for showing who a token claims to be, never for
    trusting it."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    keep = ("iss", "aud", "sub", "email", "preferred_username", "exp", "iat", "type")
    out = {k: payload[k] for k in keep if k in payload}
    if isinstance(payload.get("exp"), (int, float)):
        out["expired"] = payload["exp"] < time.time()
    return out


def fingerprint(name: str, value: str) -> Any:
    """A header value safe to log. Credentials keep enough to tell which one it was
    (an mcpe_ token's 12-character prefix, as the token list shows it, or a JWT's
    claims) and nothing that would work as the credential."""
    lower = name.lower()
    if lower == "x-mcpe-internal":
        from app.runners.builtin import INTERNAL_SECRET

        valid = secrets.compare_digest(value.encode(), INTERNAL_SECRET.encode())
        return "<valid>" if valid else "<INVALID>"
    if lower in ("authorization", "proxy-authorization"):
        scheme, _, token = value.partition(" ")
        token = token.strip()
        if token.startswith("mcpe_"):
            return f"{scheme} {token[:12]}… ({len(token)} chars)"
        claims = _jwt_claims(token)
        if claims is not None:
            return {"scheme": scheme, "jwt": claims}
        return f"{scheme} <{len(token)} chars>"
    if lower == "cf-access-jwt-assertion":
        return {"jwt": _jwt_claims(value)}
    if lower in ("cookie", "set-cookie"):
        return f"<{value.count(';') + 1} cookie(s)>"
    if any(word in lower for word in _SECRET_WORDS):
        return f"<redacted, {len(value)} chars>"
    return value


def headers_for_log(raw: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw:
        name = k.decode("latin-1")
        out[name] = fingerprint(name, v.decode("latin-1"))
    return out


# ---- request tracing -----------------------------------------------------------


def _rpc_summary(body: bytes) -> Any:
    """``initialize`` / ``tools/call whoami`` for a JSON-RPC body (or a batch)."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data]
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("method"), str):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        target = params.get("name") or params.get("uri")
        out.append(f"{item['method']} {target}" if isinstance(target, str) else item["method"])
    return out or None


class TraceMiddleware:
    """One log line (and one ``TRACES`` entry) per HTTP request; see the module doc."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(_TRACED):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        entry: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": scope["method"],
            "path": scope["path"],
            "query": scope.get("query_string", b"").decode("latin-1") or None,
            "peer": scope["client"][0] if scope.get("client") else None,
            "headers": headers_for_log(scope["headers"]),
        }

        # Buffer a small JSON body to name its JSON-RPC method, then replay it.
        content_type = dict(scope["headers"]).get(b"content-type", b"")
        buffered: list[Message] = []
        if scope["method"] == "POST" and b"json" in content_type:
            chunks: list[bytes] = []
            size = 0
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] != "http.request":
                    break
                chunks.append(message.get("body", b""))
                size += len(chunks[-1])
                if size > 64 * 1024 or not message.get("more_body", False):
                    break
            if size <= 64 * 1024:
                entry["rpc"] = _rpc_summary(b"".join(chunks))

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        body: list[bytes] = []
        state: dict[str, Any] = {"status": None, "streaming": False}

        async def watch(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                entry["status"] = message["status"]
                entry["ms"] = round((time.monotonic() - started) * 1000, 1)
                response_headers = dict(message.get("headers", []))
                state["streaming"] = b"event-stream" in response_headers.get(b"content-type", b"")
                if message["status"] < 400 or state["streaming"]:
                    _finish(entry)
            elif message["type"] == "http.response.body" and state["status"] is not None:
                if state["status"] >= 400 and not state["streaming"]:
                    if sum(map(len, body)) < _BODY_LIMIT:
                        body.append(message.get("body", b""))
                    if not message.get("more_body", False):
                        entry["error_body"] = b"".join(body)[:_BODY_LIMIT].decode("utf-8", "replace")
                        _finish(entry)
            await send(message)

        try:
            await self.app(scope, replay, watch)
        except Exception:
            entry["status"] = entry.get("status") or 500
            entry["exception"] = True
            logger.exception("unhandled error serving %s %s", scope["method"], scope["path"])
            _finish(entry)
            raise


def _finish(entry: dict[str, Any]) -> None:
    if entry.get("_done"):
        return
    entry["_done"] = True
    TRACES.append({k: v for k, v in entry.items() if k != "_done"})
    level = logging.WARNING if (entry.get("status") or 0) >= 400 else logging.DEBUG
    rpc = f" rpc={entry['rpc']}" if entry.get("rpc") else ""
    logger.log(
        level, "%s %s -> %s in %sms%s peer=%s headers=%s%s",
        entry["method"], entry["path"], entry.get("status"), entry.get("ms"), rpc,
        entry.get("peer"), json.dumps(entry["headers"], default=str),
        f" body={entry['error_body']!r}" if entry.get("error_body") else "",
    )


# ---- startup + the diagnostics bundle ------------------------------------------


def _config() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in get_settings().model_dump().items():
        if any(word in name for word in _SECRET_WORDS) and isinstance(value, str) and value:
            out[name] = "<set>"
        else:
            out[name] = str(value) if not isinstance(value, (bool, int, float, type(None))) else value
    return out


def _versions() -> dict[str, str]:
    out = {"mcpelevator": __version__, "python": platform.python_version(),
           "platform": platform.platform()}
    for package in ("fastmcp", "mcp", "fastapi", "starlette", "uvicorn", "httpx", "pyjwt"):
        try:
            out[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            pass
    return out


def log_startup() -> None:
    logger.warning(
        "debug mode is ON (MCPE_DEBUG). Logs can hold tool arguments and results. "
        "versions=%s config=%s",
        json.dumps(_versions()), json.dumps(_config(), default=str),
    )


router = APIRouter()


def _require_debug() -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/debug", dependencies=[Depends(_require_debug)])
async def bundle(request: Request) -> dict[str, Any]:
    """Everything a failure report needs, in one JSON document (admin only)."""
    from app.auth.control_plane import enforcement_enabled
    from app.auth.principal import require_admin
    from app.db import get_engine, repo
    from app.groups import registry
    from app.registry import settings as runtime_settings

    with Session(get_engine()) as session:
        require_admin(request, session)
        settings = runtime_settings.read_all(session)
        enforced = enforcement_enabled(session)
        groups = registry.read(session)
        rows = repo.list_servers(session)
        users = [{"id": u.id, "name": u.name, "role": u.role, "control_mcp": u.control_mcp}
                 for u in repo.list_users(session)]
    sup = request.app.state.supervisor
    hub = request.app.state.groups
    servers = []
    for row in rows:
        unit = sup.unit(row.id)
        servers.append({
            "id": row.id, "slug": row.slug, "name": row.name, "runner": row.runner,
            "enabled": row.enabled, "auth_provider": row.auth_provider,
            "owner_id": row.owner_id, "env_keys": sorted((row.env or {}).keys()),
            "state": unit.state if unit is not None else "stopped",
            "port": unit.port if unit is not None else None,
            "last_error": unit.last_error if unit is not None else None,
            "log_tail": unit.logs.snapshot()[-60:] if unit is not None else [],
        })
    return {
        "versions": _versions(),
        "config": _config(),
        "control_plane_auth_enforced": enforced,
        "runtime_settings": settings,
        "users": users,
        "servers": servers,
        "groups": {name: {"members": members, "mounted": hub.mounted_members(name)}
                   for name, members in groups.items()},
        "traces": list(TRACES),
        "logs": list(RECORDS),
    }
