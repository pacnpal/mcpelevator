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


def test_api_rejects_spoofed_loopback_host_from_remote_client():
    """P1 fix: an off-host client (e.g. a Docker 0.0.0.0 bind reachable from the
    LAN) must not pass the guard by sending Host: localhost. Loopback Hosts are
    trusted only when the peer actually connects from loopback."""
    with TestClient(app, client=("203.0.113.5", 9999)) as client:  # non-loopback peer
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"bind_mode": "local", "allowed_hosts": []})
        for spoof in ("localhost", "127.0.0.1", "[::1]:8080"):
            r = client.get("/api/health", headers={"host": spoof})
            assert r.status_code == 403, (spoof, r.status_code)


def test_summary_exposes_effective_auth():
    """The card snippets need the effective auth, so the summary resolves
    `inherit` to the global default and reports `none`/`bearer`."""
    with TestClient(app) as client:
        h = {"host": "127.0.0.1"}
        created: list[str] = []
        try:
            r = client.post(
                "/api/servers", json={"name": "b", "command": "echo", "auth_provider": "bearer"}, headers=h
            )
            assert r.status_code == 201, r.text
            created.append(r.json()["id"])
            assert r.json()["auth"] == "bearer"
            r2 = client.post(
                "/api/servers", json={"name": "n", "command": "echo", "auth_provider": "none"}, headers=h
            )
            created.append(r2.json()["id"])
            assert r2.json()["auth"] == "none"
        finally:
            for sid in created:
                client.delete(f"/api/servers/{sid}", headers=h)


def test_summary_normalizes_legacy_auth_provider():
    """A legacy row with a non-canonical auth_provider (the old schema accepted any
    str) must not 500 GET /api/servers — the effective auth is coerced to the Literal."""
    from sqlalchemy import text

    with TestClient(app) as client:
        h = {"host": "127.0.0.1"}
        sid = client.post(
            "/api/servers", json={"name": "lg", "command": "echo", "auth_provider": "bearer"}, headers=h
        ).json()["id"]
        try:
            with Session(get_engine()) as s:  # simulate a legacy value the old str schema allowed
                s.execute(text("UPDATE server SET auth_provider='Bearer' WHERE id=:i"), {"i": sid})
                s.commit()
            listing = client.get("/api/servers", headers=h)
            assert listing.status_code == 200, listing.text
            row = next(x for x in listing.json() if x["id"] == sid)
            assert row["auth"] == "bearer"  # "Bearer" coerced, no response-validation 500
        finally:
            client.delete(f"/api/servers/{sid}", headers=h)


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


def test_api_trusts_docker_gateway_when_configured(monkeypatch):
    """With MCPE_TRUSTED_PROXIES = the compose gateway /32, a request forwarded by
    that gateway may use Host: localhost (a fresh `docker compose up` reaching the
    loopback-published port), but a sibling container on the same network cannot."""
    from types import SimpleNamespace

    from app.auth import middleware

    public = middleware.get_settings().public_host
    monkeypatch.setattr(
        middleware,
        "get_settings",
        lambda: SimpleNamespace(trusted_proxies="172.20.0.1/32", public_host=public),
    )
    with TestClient(app, client=("172.20.0.1", 5000)) as gateway:  # the gateway -> trusted
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"bind_mode": "local", "allowed_hosts": []})
        assert gateway.get("/api/health", headers={"host": "localhost"}).status_code == 200
    with TestClient(app, client=("172.20.0.5", 5000)) as sibling:  # a sibling container
        assert sibling.get("/api/health", headers={"host": "localhost"}).status_code == 403
