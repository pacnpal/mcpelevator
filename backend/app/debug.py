"""Debug mode (``MCPE_DEBUG=true``): one switch for everything a failure report needs.
It is as verbose as it can be on purpose. When it's on:

- Every logger in the process logs at TRACE (below DEBUG) to stderr with a timestamp,
  logger name and thread: mcpelevator itself, FastMCP, the MCP SDK (every JSON-RPC
  message), httpx and httpcore (every outbound connection), uvicorn (every connection),
  SSE framing, and every SQL statement (bound values hidden). Bridges inherit the
  variable, so each server's own log (the one the UI shows) gets the same detail.
- Every HTTP request is traced: a line when it arrives (method, path, query, peer,
  headers, body), a line when the response starts (status, headers), a line per
  streamed event (so an MCP error inside an SSE stream shows up), and a summary with
  the JSON-RPC method, tool name, status and timing. Credentials are reduced to
  something you can match but not reuse, and secret-looking values in bodies are
  scrubbed.
- Crashes don't go quiet: uncaught exceptions in any thread, unhandled errors while
  serving a request, faulthandler tracebacks for hard crashes, and asyncio's debug
  mode (never-awaited coroutines, callbacks that block the loop over 100ms).
- The latest traces and log records are kept in memory and served, with the version,
  effective config, runtime settings, users and every server's state and log tail,
  at ``GET /api/debug`` (admin only). One call gives a whole picture to paste.

When it's off, none of this runs and ``/api/debug`` 404s. Debug output holds tool
arguments and results, so leave it off unless you're chasing something.
"""

from __future__ import annotations

import base64
import collections
import faulthandler
import json
import logging
import platform
import re
import secrets
import sys
import threading
import time
from importlib import metadata
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__
from app.config import get_settings

logger = logging.getLogger("app.debug")

TRACES: collections.deque = collections.deque(maxlen=500)
RECORDS: collections.deque = collections.deque(maxlen=5000)

TRACE = 5  # uvicorn's level below DEBUG (connection lifecycle)
_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s"
# INFO is where SQLAlchemy logs each statement (the engine hides bound values); DEBUG adds every
# fetched row, which is the data itself, secrets included, rather than what happened to it.
_LEVELS = {"sqlalchemy.engine": logging.INFO, "sqlalchemy.pool": logging.DEBUG}
_SECRET_WORDS = ("token", "secret", "password", "key", "assertion", "cookie", "authorization")
_BODY_LIMIT = 16384
_CHUNK_LIMIT = 4096
# The API, server and group endpoints, and OAuth discovery; not the SPA's assets.
_TRACED = ("/api", "/s/", "/g/", "/.well-known", "/mcp", "/rest")


def enabled() -> bool:
    return get_settings().debug


class _Scrubbed(logging.Formatter):
    """Every line, from any library, goes through ``scrub``: SQL parameters carry a
    server's env, and the MCP SDK logs tool arguments verbatim."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class _ScrubFilter(logging.Filter):
    """Installed on every handler that exists when debug mode starts, not just ours:
    a library's own handler (the bridge's upstream log, uvicorn's) would otherwise
    write the unscrubbed line before the record reaches the root."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        # Formatters reuse exc_text when it's set, so a scrubbed traceback here is the
        # one every handler prints.
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


_RECORD_LIMIT = 8192  # per retained record: the ring caps the count, this the size


class _Ring(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            if len(line) > _RECORD_LIMIT:
                line = line[:_RECORD_LIMIT] + f"… ({len(line)} chars)"
            RECORDS.append(line)
        except Exception:  # a log handler must never raise into the caller
            pass


def _log_uncaught(exc_type, exc, tb) -> None:
    logging.getLogger("app.debug").critical("uncaught exception", exc_info=(exc_type, exc, tb))


def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
    logging.getLogger("app.debug").critical(
        "uncaught exception in thread %s", getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def configure_logging(*, keep: bool) -> None:
    """Turn on TRACE logging for this whole process. ``keep`` also records into the
    ring that ``/api/debug`` serves (the control plane; a bridge has no such route)."""
    root = logging.getLogger()
    if any(isinstance(h, _Ring) or getattr(h, "_mcpe_debug", False) for h in root.handlers):
        return  # already on (a second app in the same process, e.g. tests)
    logging.addLevelName(TRACE, "TRACE")
    root.setLevel(TRACE)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if keep:
        handlers.append(_Ring())
    for handler in handlers:
        handler._mcpe_debug = True  # type: ignore[attr-defined]
        handler.setFormatter(_Scrubbed(_FORMAT))
        root.addHandler(handler)
    # Libraries set their own levels (uvicorn INFO, fastmcp its own handler); open
    # every logger that exists now, and let later ones inherit the root's TRACE.
    for name in list(logging.root.manager.loggerDict):
        log = logging.getLogger(name)
        log.setLevel(logging.NOTSET)
        if name == "fastmcp" or name.startswith("fastmcp."):
            log.handlers.clear()  # fastmcp's own rich handler: route through ours instead
            log.propagate = True
    for log in [root, *(logging.getLogger(n) for n in logging.root.manager.loggerDict)]:
        for handler in log.handlers:
            if not any(isinstance(f, _ScrubFilter) for f in handler.filters):
                handler.addFilter(_ScrubFilter())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(TRACE)
    for name, level in _LEVELS.items():
        logging.getLogger(name).setLevel(level)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught
    faulthandler.enable()  # a hard crash (segfault, deadlock dump) still leaves a traceback


def uvicorn_logging() -> dict[str, Any]:
    """Extra ``uvicorn.run`` kwargs for a process that already ran ``configure_logging``.
    uvicorn's default ``log_config`` would install fresh, unscrubbed handlers on its own
    loggers; ``None`` leaves them propagating to the scrubbed root handlers instead."""
    return {"log_config": None} if enabled() else {}


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


def query_for_log(raw: bytes) -> dict[str, str] | None:
    """Query parameters safe to log: an OAuth callback's ``code`` and ``state``, and
    anything secret-named (``access_token``, ``api_key``), keep only their length."""
    out = {}
    for name, value in parse_qsl(raw.decode("latin-1"), keep_blank_values=True):
        lower = name.lower()
        secret = lower in ("code", "state") or any(word in lower for word in _SECRET_WORDS)
        out[name] = f"<redacted, {len(value)} chars>" if secret else scrub(value)
    return out or None


def headers_for_log(raw: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw:
        name = k.decode("latin-1")
        out[name] = fingerprint(name, v.decode("latin-1"))
    return out


# ---- request tracing -----------------------------------------------------------

_MCPE_TOKEN = re.compile(r"mcpe_[A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# A secret-looking field in JSON ("API_KEY": "...") or a Python repr ('token': '...').
# A value cut off by a size limit has no closing quote, so the end of the text
# closes it too: a truncated body must not keep the secret it was cut through.
_SECRET_FIELD = re.compile(
    r"""(["'][^"']*(?:token|secret|password|key|authorization|assertion|cookie)[^"']*["']"""
    r"""\s*:\s*)(["'])(?:\\.|\\\Z|(?!\2)[^\\])*(?:\2|\Z)""",
    re.IGNORECASE,
)
# Any other credential after an auth scheme (Authorization: Bearer ghp_..., an
# upstream's error echoing its header). mcpe_ tokens keep their prefix, see above.
# A value of lowercase letters alone is a word, so prose ("bearer provider") stays.
_AUTH_SCHEME = re.compile(
    r"\b((?i:bearer|basic))(\s+)(?!mcpe_)(?![a-z]+(?![A-Za-z0-9._~+/=-]))[A-Za-z0-9._~+/=-]{8,}"
)

# The user:password part of a URL (postgres://alice:s3cr3t@db/app), encoded or not.
_URL_USERINFO = re.compile(r"""\b([a-z][a-z0-9+.-]*://)[^\s/@"'<>]+@""", re.IGNORECASE)
# The end of one server-sent event: a blank line, LF or CRLF.
_EVENT_END = re.compile(rb"\r?\n\r?\n")

# A secret query parameter inside any line: in a URL (uvicorn's access log prints the
# full request target) or at the start of a quoted raw query string (uvicorn's ASGI
# trace prints 'query_string': b'code=...'). An OAuth callback's code/state,
# access_token=, api_key=, or the first field of a form body.
_SECRET_QUERY = re.compile(
    r"((?:^|[?&'\"])(?:code|state|[^=&\s\"']*(?:token|secret|password|key|assertion)[^=&\s\"']*)=)"
    r"[^&\s\"']*",
    re.IGNORECASE,
)


def scrub(text: str) -> str:
    """A body safe to log: mcpe_ tokens cut to their 12-character prefix, JWTs and
    the values of secret-looking JSON fields (an env var named *_TOKEN or API_KEY, a
    freshly minted token) replaced."""
    text = _MCPE_TOKEN.sub(lambda m: m.group(0)[:12] + "…", text)
    text = _JWT.sub("<jwt>", text)
    text = _SECRET_QUERY.sub(r"\1<redacted>", text)
    text = _AUTH_SCHEME.sub(r"\1\2<redacted>", text)
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    return _SECRET_FIELD.sub(r"\1\2<redacted>\2", text)


def _body_text(chunks: list[bytes], limit: int) -> str:
    """The first ``limit`` bytes, scrubbed. Slices before joining, so a single huge
    chunk is never copied whole."""
    total = sum(map(len, chunks))
    prefix, left = [], limit
    for chunk in chunks:
        if left <= 0:
            break
        prefix.append(chunk[:left])
        left -= len(prefix[-1])
    text = scrub(b"".join(prefix).decode("utf-8", "replace"))
    return text + f"… ({total} bytes)" if total > limit else text


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
        out.append(scrub(f"{item['method']} {target}") if isinstance(target, str) else item["method"])
    return out or None


class TraceMiddleware:
    """Traces every HTTP request; see the module doc."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(_TRACED):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        rid = secrets.token_hex(4)  # ties a request's lines together in a busy log
        entry: dict[str, Any] = {
            "id": rid,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": scope["method"],
            "path": scope["path"],
            "query": query_for_log(scope.get("query_string", b"")),
            "peer": scope["client"][0] if scope.get("client") else None,
            "headers": headers_for_log(scope["headers"]),
        }

        # Read the start of the body (up to _BODY_LIMIT, so a huge upload can't fill
        # memory), log it, then replay what was read and hand the rest of the stream
        # to the app untouched.
        buffered: list[Message] = []
        chunks: list[bytes] = []
        complete = False
        while sum(map(len, chunks)) <= _BODY_LIMIT:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                complete = True
                break
        if any(chunks):
            entry["body"] = _body_text(chunks, _BODY_LIMIT) + ("" if complete else " …(more)")
            if complete and sum(map(len, chunks)) <= 64 * 1024:
                entry["rpc"] = _rpc_summary(b"".join(chunks))
        logger.debug(
            "[%s] -> %s %s query=%s peer=%s headers=%s body=%s", rid, entry["method"],
            entry["path"], entry["query"], entry["peer"],
            json.dumps(entry["headers"], default=str), entry.get("body"),
        )

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        response: list[bytes] = []
        state: dict[str, Any] = {"streaming": False, "sent": 0, "pending": b"", "skipping": False}

        def log_events(chunk: bytes, end: bool) -> None:
            # A stream is logged one whole event at a time, never a raw chunk:
            # chunk boundaries are arbitrary, and a secret field split across two
            # would slip past scrub() in both halves.
            *done, state["pending"] = _EVENT_END.split(state["pending"] + chunk)
            if end and state["pending"]:
                done, state["pending"] = done + [state["pending"]], b""
            for i, event in enumerate(done):
                if i == 0 and state["skipping"]:
                    state["skipping"] = False  # the tail of an event too long to hold
                elif event:
                    logger.debug("[%s] <- event %s", rid, _body_text([event], _CHUNK_LIMIT))
            if len(state["pending"]) > _BODY_LIMIT:
                if not state["skipping"]:
                    logger.debug("[%s] <- event %s …(rest not logged)", rid,
                                 _body_text([state["pending"]], _CHUNK_LIMIT))
                # Keep only enough to spot an event end split across chunks.
                state["pending"], state["skipping"] = state["pending"][-3:], True

        async def watch(message: Message) -> None:
            if message["type"] == "http.response.start":
                entry["status"] = message["status"]
                entry["ms"] = round((time.monotonic() - started) * 1000, 1)
                response_headers = headers_for_log(message.get("headers", []))
                state["streaming"] = "event-stream" in str(response_headers.get("content-type", ""))
                logger.debug("[%s] <- %s %s in %sms headers=%s", rid, message["status"],
                             entry["path"], entry["ms"], json.dumps(response_headers, default=str))
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                state["sent"] += len(chunk)
                if state["streaming"]:
                    log_events(chunk, end=not message.get("more_body", False))
                elif (room := _BODY_LIMIT + 1 - sum(map(len, response))) > 0:
                    response.append(chunk[:room])  # keep a prefix, never the whole result
                if not message.get("more_body", False):
                    if not state["streaming"]:
                        entry["response_body"] = _body_text(response, _BODY_LIMIT)
                    entry["bytes"] = state["sent"]
                    _finish(entry, started)
            await send(message)

        try:
            await self.app(scope, replay, watch)
        except Exception:
            entry["status"] = entry.get("status") or 500
            entry["exception"] = True
            logger.exception("[%s] unhandled error serving %s %s", rid, scope["method"], scope["path"])
            _finish(entry, started)
            raise
        finally:
            if "total_ms" not in entry:
                entry["disconnected"] = True  # the client left before the response ended
                _finish(entry, started)


def _finish(entry: dict[str, Any], started: float) -> None:
    if "total_ms" in entry:
        return
    entry["total_ms"] = round((time.monotonic() - started) * 1000, 1)
    TRACES.append(dict(entry))
    level = logging.WARNING if (entry.get("status") or 0) >= 400 else logging.INFO
    rpc = f" rpc={entry['rpc']}" if entry.get("rpc") else ""
    body = f" response={entry['response_body']!r}" if entry.get("response_body") else ""
    logger.log(
        level, "[%s] %s %s -> %s first byte %sms, done %sms, %s bytes%s%s",
        entry["id"], entry["method"], entry["path"], entry.get("status"), entry.get("ms"),
        entry["total_ms"], entry.get("bytes", 0), rpc, body,
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
            # A server's own output never went through the log scrubber.
            "last_error": scrub(unit.last_error) if unit is not None and unit.last_error else None,
            "log_tail": [scrub(line) for line in unit.logs.snapshot()[-60:]] if unit is not None else [],
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
