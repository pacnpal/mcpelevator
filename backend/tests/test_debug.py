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
