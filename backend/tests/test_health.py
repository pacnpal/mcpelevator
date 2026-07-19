"""Per-server health endpoints: /api/health/{slug} and /api/health/summary.

These let a load balancer / client check whether a specific proxied server is
accepting requests. The responses are deliberately COARSE — only pass/fail
readiness, never state/last_error/inventory detail. The inventory-bearing
endpoints are control-plane gated when auth enforcement is on. Servers are
created disabled (no subprocess); the supervisor endpoint is stubbed to
simulate running/not-running deterministically.
"""

from __future__ import annotations

from functools import partial

from fastapi.testclient import TestClient

from conftest import LOOPBACK, create_server

from app.main import app

# Fields that must never appear in a public health response — they're privileged
# diagnostics/inventory that belong behind the gated control plane (/api/servers).
_PRIVILEGED_FIELDS = {"state", "last_error", "enabled", "pid", "port"}


_create_server = partial(create_server, name="h")


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
            # not running -> 503, flat coarse body
            client.app.state.supervisor.endpoint = lambda s: None
            down = client.get(f"/api/health/{slug}", headers=LOOPBACK)
            assert down.status_code == 503
            assert down.json() == {"slug": slug, "running": False, "status": "unavailable"}

            # running -> 200
            client.app.state.supervisor.endpoint = lambda s: ("backend", 9000)
            up = client.get(f"/api/health/{slug}", headers=LOOPBACK)
            assert up.status_code == 200
            assert up.json() == {"slug": slug, "running": True, "status": "ok"}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_slug_200_and_503_share_a_flat_coarse_schema():
    """Both the 200 and the 503 carry the same flat {slug, running, status} body —
    not nested under ``detail`` — so one schema parses either outcome."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            client.app.state.supervisor.endpoint = lambda s: ("backend", 9000)
            ok = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            assert ok.status_code == 200
            assert set(ok.json()) == {"slug", "running", "status"}

            client.app.state.supervisor.endpoint = lambda s: None
            down = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            assert down.status_code == 503
            assert set(down.json()) == {"slug", "running", "status"}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_public_health_does_not_leak_privileged_fields():
    """Security: per-server health responses must not expose privileged
    state, raw last_error, enabled, pid, or port — those stay behind the gated
    control plane. Checked on the per-server 200/503 and the summary rows."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            for endpoint in (lambda s: ("backend", 9000), lambda s: None):
                client.app.state.supervisor.endpoint = endpoint
                body = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK).json()
                assert _PRIVILEGED_FIELDS.isdisjoint(body), body

            summary = client.get("/api/health/summary", headers=LOOPBACK).json()
            for row in summary["servers"]:
                assert _PRIVILEGED_FIELDS.isdisjoint(row), row
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
            assert row == {"slug": srv["slug"], "running": False}

            # enable it while its backend is down -> overall degrades
            client.post(f"/api/servers/{srv['id']}/enable", headers=LOOPBACK)
            r2 = client.get("/api/health/summary", headers=LOOPBACK)
            assert r2.json()["status"] == "degraded"
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_summary_all_enabled_running_is_ok():
    """When every enabled server is running the overall status must be 'ok'."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            # enable the server, then make the endpoint report it as running
            client.post(f"/api/servers/{srv['id']}/enable", headers=LOOPBACK)
            client.app.state.supervisor.endpoint = lambda s: ("backend", 9000)
            r = client.get("/api/health/summary", headers=LOOPBACK)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            row = next(h for h in data["servers"] if h["slug"] == srv["slug"])
            assert row["running"] is True
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_endpoint_is_public_not_gated():
    """The top-level liveness endpoint must stay reachable without auth."""
    with TestClient(app) as client:
        assert client.get("/api/health", headers=LOOPBACK).status_code == 200
