"""Per-server health endpoints: /api/health/{slug} and /api/health/summary.

These let a load balancer / client check whether a specific proxied server is
accepting requests. Servers are created disabled (no subprocess); the supervisor
endpoint is stubbed to simulate running/not-running deterministically.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

LOOPBACK = {"host": "127.0.0.1"}


def _create_server(client: TestClient) -> dict:
    r = client.post(
        "/api/servers", json={"name": "h", "command": "echo"}, headers=LOOPBACK
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_control_plane_health_is_unchanged():
    with TestClient(app) as client:
        r = client.get("/api/health", headers=LOOPBACK)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "version" in r.json()


def test_health_slug_unknown_is_404():
    with TestClient(app) as client:
        assert client.get("/api/health/nope", headers=LOOPBACK).status_code == 404


def test_health_slug_reports_running_and_down():
    with TestClient(app) as client:
        srv = _create_server(client)
        slug = srv["slug"]
        try:
            # not running -> 503, with the diagnostic body
            client.app.state.supervisor.endpoint = lambda s: None
            down = client.get(f"/api/health/{slug}", headers=LOOPBACK)
            assert down.status_code == 503

            # running -> 200
            client.app.state.supervisor.endpoint = lambda s: ("backend", 9000)
            up = client.get(f"/api/health/{slug}", headers=LOOPBACK)
            assert up.status_code == 200
            body = up.json()
            assert body["slug"] == slug
            assert body["running"] is True
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_summary_lists_servers_and_overall_status():
    with TestClient(app) as client:
        srv = _create_server(client)  # disabled by default
        try:
            # a disabled server is intentionally down -> overall still ok
            client.app.state.supervisor.endpoint = lambda s: None
            r = client.get("/api/health/summary", headers=LOOPBACK)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            row = next(h for h in data["servers"] if h["slug"] == srv["slug"])
            assert row["enabled"] is False
            assert row["running"] is False

            # enable it while its backend is down -> overall degrades
            client.post(f"/api/servers/{srv['id']}/enable", headers=LOOPBACK)
            r2 = client.get("/api/health/summary", headers=LOOPBACK)
            assert r2.json()["status"] == "degraded"
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)
