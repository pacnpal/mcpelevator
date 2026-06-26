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


def test_api_loopback_only_in_local_mode():
    """Local mode still rejects a non-loopback Host (DNS-rebinding defense); only
    loopback — the sole legitimate way to reach a local deployment — passes."""
    with TestClient(app) as client:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"bind_mode": "local", "allowed_hosts": []})
        assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403
        assert client.get("/api/health", headers={"host": "127.0.0.1"}).status_code == 200
        assert client.get("/api/health", headers={"host": "localhost:8080"}).status_code == 200


def test_summary_exposes_effective_auth():
    """The card snippets need the effective auth, so the summary resolves
    `inherit` to the global default and reports `none`/`bearer`."""
    with TestClient(app) as client:
        h = {"host": "127.0.0.1"}
        r = client.post(
            "/api/servers", json={"name": "b", "command": "echo", "auth_provider": "bearer"}, headers=h
        )
        assert r.status_code == 201, r.text
        assert r.json()["auth"] == "bearer"
        r2 = client.post(
            "/api/servers", json={"name": "n", "command": "echo", "auth_provider": "none"}, headers=h
        )
        assert r2.json()["auth"] == "none"


def test_create_server_rejects_unknown_auth_provider():
    """A malformed auth_provider (trailing space / wrong case / unknown) is rejected
    at the API boundary (422), not stored and later failed-closed at request time."""
    with TestClient(app) as client:
        for bad in ("bearer ", "Bearer", "basic"):
            r = client.post(
                "/api/servers",
                json={"name": "x", "command": "echo", "auth_provider": bad},
                headers={"host": "127.0.0.1"},
            )
            assert r.status_code == 422, (bad, r.status_code, r.text)
