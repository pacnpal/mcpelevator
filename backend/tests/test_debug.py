"""Debug mode (MCPE_DEBUG): credential fingerprints, the request trace, and the
admin-only diagnostics bundle."""

from __future__ import annotations

import base64
import json

import logging

import pytest
from fastapi.testclient import TestClient

from conftest import LOOPBACK, mint_token

from app import debug
from app.config import get_settings
from app.main import create_app
from app.runners.builtin import INTERNAL_SECRET


@pytest.fixture(autouse=True)
def _restore_logging():
    """Debug mode reconfigures every logger and the excepthooks process-wide; put them
    back for later tests."""
    import faulthandler
    import sys
    import threading

    names = ["", *logging.root.manager.loggerDict]
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers),
                 logging.getLogger(n).propagate) for n in names}
    hooks = (sys.excepthook, threading.excepthook, faulthandler.is_enabled())
    yield
    for name in logging.root.manager.loggerDict:
        if name not in saved:
            logging.getLogger(name).setLevel(logging.NOTSET)
    for n, (level, handlers, propagate) in saved.items():
        log = logging.getLogger(n)
        log.setLevel(level)
        log.handlers[:] = handlers
        log.propagate = propagate
    for log in [logging.getLogger(), *(logging.getLogger(n) for n in logging.root.manager.loggerDict)]:
        for handler in log.handlers:
            for f in [f for f in handler.filters if isinstance(f, debug._ScrubFilter)]:
                handler.removeFilter(f)
    sys.excepthook, threading.excepthook = hooks[0], hooks[1]
    if not hooks[2]:
        faulthandler.disable()


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


def test_fingerprints_never_log_a_usable_credential():
    token = "mcpe_" + "x" * 40
    assert debug.fingerprint("Authorization", f"Bearer {token}") == "Bearer mcpe_xxxxxxx… (45 chars)"
    jwt = debug.fingerprint("authorization", f"Bearer {_jwt({'iss': 'https://idp', 'sub': 'ada', 'exp': 1})}")
    assert jwt["jwt"] == {"iss": "https://idp", "sub": "ada", "exp": 1, "expired": True}
    assert debug.fingerprint("x-mcpe-internal", INTERNAL_SECRET) == "<valid>"
    assert debug.fingerprint("x-mcpe-internal", "guess") == "<INVALID>"
    assert debug.fingerprint("cookie", "a=1; b=2") == "<2 cookie(s)>"
    assert debug.fingerprint("x-api-key", "sekrit") == "<redacted, 6 chars>"
    assert debug.fingerprint("accept", "text/plain") == "text/plain"


def test_scrub_hides_secrets_in_bodies_and_library_logs():
    token = "mcpe_" + "y" * 40
    out = debug.scrub(f'{{"token": "{token}", "env": {{"GITHUB_TOKEN": "ghp_1", "MODE": "x"}}}}')
    assert token not in out and "ghp_1" not in out and '"MODE": "x"' in out
    assert "sk-1" not in debug.scrub("arguments={'api_key': 'sk-1', 'q': 'cats'}")
    assert "<jwt>" in debug.scrub(f"Bearer {_jwt({'sub': 'a'})}")


def test_query_strings_are_scrubbed():
    out = debug.query_for_log(b"code=abc123&state=xyz&api_key=k&page=2&q=mcpe_" + b"z" * 20)
    assert out["code"] == "<redacted, 6 chars>" and out["state"] == "<redacted, 3 chars>"
    assert out["api_key"] == "<redacted, 1 chars>" and out["page"] == "2"
    assert "z" * 20 not in out["q"]
    assert debug.query_for_log(b"") is None


def test_handlers_installed_before_debug_mode_are_scrubbed():
    import io

    stream = io.StringIO()
    early = logging.getLogger("mcpelevator.test.early")
    early.addHandler(logging.StreamHandler(stream))
    early.propagate = False
    debug.configure_logging(keep=False)
    early.warning("token=%s", "mcpe_" + "q" * 30)
    assert "q" * 30 not in stream.getvalue()


def test_scrub_filter_covers_tracebacks_and_rpc_targets():
    import io

    stream = io.StringIO()
    early = logging.getLogger("mcpelevator.test.exc")
    early.addHandler(logging.StreamHandler(stream))
    early.propagate = False
    debug.configure_logging(keep=False)
    secret = "mcpe_" + "w" * 30
    try:
        raise RuntimeError(f"bad token {secret}")
    except RuntimeError:
        early.exception("failed")
    assert "w" * 30 not in stream.getvalue() and "RuntimeError" in stream.getvalue()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                       "params": {"uri": f"x://{secret}"}}).encode()
    assert "w" * 30 not in json.dumps(debug._rpc_summary(body))


def test_trace_keeps_only_a_prefix_of_a_large_response():
    import asyncio

    big = b"y" * 2_000_000

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": big})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/api/big", "headers": [], "query_string": b""}
    asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    trace = debug.TRACES[-1]
    assert trace["bytes"] == 2_000_000 and len(trace["response_body"]) < 20000


def test_trace_bounds_a_large_request_body():
    import asyncio

    seen = []

    async def app(scope, receive, send):
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        seen.append(total)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunk = b"x" * 10000
    messages = [{"type": "http.request", "body": chunk, "more_body": i < 99} for i in range(100)]
    pulled = []

    async def receive():
        pulled.append(1)
        return messages[len(pulled) - 1]

    async def send(message):
        pass

    scope = {"type": "http", "method": "POST", "path": "/api/x", "headers": [], "query_string": b""}
    asyncio.run(debug.TraceMiddleware(app)(scope, receive, send))
    assert seen == [1_000_000]  # the app still gets the whole body
    trace = debug.TRACES[-1]
    assert trace["body"].endswith("…(more)") and len(trace["body"]) < 20000


def test_bundle_needs_an_admin_when_auth_is_enforced(monkeypatch):
    from sqlmodel import Session

    from app.db import get_engine
    from app.registry import settings as runtime_settings

    monkeypatch.setattr(get_settings(), "debug", True)
    with Session(get_engine()) as s:
        previous = runtime_settings.control_plane_auth(s)
        runtime_settings.write(s, {"control_plane_auth": "always"})
    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/debug", headers=LOOPBACK).status_code == 401
    finally:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": previous})


def test_bundle_is_404_unless_debug(monkeypatch):
    monkeypatch.setattr(get_settings(), "debug", False)
    with TestClient(create_app()) as client:
        assert client.get("/api/debug", headers=LOOPBACK).status_code == 404


def test_debug_traces_requests_and_serves_the_bundle(monkeypatch):
    monkeypatch.setattr(get_settings(), "debug", True)
    debug.TRACES.clear()
    with TestClient(create_app()) as client:
        token = mint_token("control")
        r = client.post("/s/nope/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={**LOOPBACK, "authorization": f"Bearer {token}"})
        assert r.status_code == 404
        trace = next(t for t in debug.TRACES if t["path"] == "/s/nope/mcp")
        assert trace["status"] == 404 and trace["rpc"] == ["tools/list"]
        assert trace["response_body"] == "unknown server"
        assert token not in json.dumps(trace)

        minted = client.post("/api/tokens", json={"name": "x", "scope": "all"}, headers=LOOPBACK)
        trace = next(t for t in reversed(debug.TRACES) if t["path"] == "/api/tokens")
        assert minted.json()["token"] not in trace["response_body"]

        bundle = client.get("/api/debug", headers=LOOPBACK).json()
        assert {"versions", "config", "servers", "groups", "traces", "logs"} <= set(bundle)
        assert bundle["config"]["debug"] is True
