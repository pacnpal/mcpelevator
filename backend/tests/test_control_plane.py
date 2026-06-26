"""The /api control plane is guarded by the Host/Origin allowlist in expose mode.

Per-request bearer auth on /api is a deferred v1 item; this proves the partial
hardening: when ``bind_mode=expose`` an off-allowlist Host cannot reach /api,
while allowlisted hosts and loopback can (the SPA is served same-origin).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app.main import app
from app.registry import settings as runtime_settings


def test_api_allowlist_enforced_in_expose_mode():
    with TestClient(app) as client:
        try:
            with Session(get_engine()) as s:
                runtime_settings.write(
                    s, {"bind_mode": "expose", "allowed_hosts": ["mcp.example.com"]}
                )
            assert client.get("/api/health", headers={"host": "evil.com"}).status_code == 403
            assert client.get("/api/health", headers={"host": "mcp.example.com"}).status_code == 200
            assert client.get("/api/health", headers={"host": "127.0.0.1"}).status_code == 200
        finally:
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"bind_mode": "local", "allowed_hosts": []})


def test_api_open_in_local_mode():
    """Default (local) mode does NOT enforce the allowlist on /api — any Host
    passes, so the same-origin SPA keeps working."""
    with TestClient(app) as client:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"bind_mode": "local", "allowed_hosts": []})
        assert client.get("/api/health", headers={"host": "anything.example"}).status_code == 200
