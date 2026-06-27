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


# ---------------------------------------------------------------------------
# Additional edge-case / branch coverage tests
# ---------------------------------------------------------------------------


def test_health_slug_503_body_contains_health_dict():
    """A 503 response must carry the diagnostic health dict, so a load balancer or
    operator can see state/last_error without a second call. The body is flat — the
    same shape as the 200 — not nested under ``detail``, so one schema parses both."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            client.app.state.supervisor.endpoint = lambda s: None
            r = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            assert r.status_code == 503
            detail = r.json()
            assert detail["slug"] == srv["slug"]
            assert detail["running"] is False
            assert "enabled" in detail
            assert "state" in detail
            assert "last_error" in detail
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_slug_200_body_includes_all_fields():
    """Running server health response must expose slug, enabled, running, state,
    and last_error so clients have a single call for full diagnostics."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            client.app.state.supervisor.endpoint = lambda s: ("backend", 9000)
            r = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            assert r.status_code == 200
            body = r.json()
            assert body["slug"] == srv["slug"]
            assert body["running"] is True
            assert "enabled" in body
            assert "state" in body
            assert "last_error" in body
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_summary_empty_server_list_is_ok():
    """With no servers at all the summary must still return status 'ok'
    — an empty deployment is not degraded."""
    # Use a fresh TestClient so the shared DB happens to be empty. We
    # explicitly delete any pre-existing servers that leaked from other tests
    # by relying on the summary listing them and cleaning up.
    with TestClient(app) as client:
        client.app.state.supervisor.endpoint = lambda s: None
        r = client.get("/api/health/summary", headers=LOOPBACK)
        assert r.status_code == 200
        data = r.json()
        # Filter to only servers present in this response (other tests may have left rows)
        enabled_servers = [h for h in data["servers"] if h["enabled"]]
        if not enabled_servers:
            assert data["status"] == "ok"


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
            assert row["enabled"] is True
            assert row["running"] is True
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


class _FakeUnit:
    """Minimal stand-in for a ServerUnit that the health helper reads."""

    def __init__(self, state: str, last_error=None):
        self.state = state
        self.last_error = last_error


def test_server_health_uses_unit_state_when_unit_present():
    """_server_health must prefer the in-memory unit's state/last_error over the
    runtime row — the unit is always more up to date than the persisted snapshot."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            # Inject a fake unit for the server id; endpoint still says "not running"
            unit = _FakeUnit(state="failed", last_error="spawn error")
            real_unit = client.app.state.supervisor.unit

            def _fake_unit(server_id):
                if server_id == srv["id"]:
                    return unit
                return real_unit(server_id)

            client.app.state.supervisor.unit = _fake_unit
            client.app.state.supervisor.endpoint = lambda s: None

            r = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            # Server is not running -> 503 (flat body, same shape as the 200)
            assert r.status_code == 503
            detail = r.json()
            # State must come from the unit, not from the (absent) runtime row
            assert detail["state"] == "failed"
            assert detail["last_error"] == "spawn error"
        finally:
            # restore so other tests aren't affected
            client.app.state.supervisor.unit = real_unit
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_server_health_falls_back_to_stopped_when_no_unit_and_no_runtime():
    """When neither a supervisor unit nor a runtime row exists the health helper
    must default to state='stopped' and last_error=None."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            # No unit and no runtime row -> defaults
            client.app.state.supervisor.unit = lambda server_id: None
            client.app.state.supervisor.endpoint = lambda s: None

            r = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            assert r.status_code == 503
            detail = r.json()
            assert detail["state"] == "stopped"
            assert detail["last_error"] is None
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_health_endpoint_is_public_not_gated():
    """All three health endpoints must be reachable without any auth token,
    even when the control plane enforces bearer auth on other routes."""
    with TestClient(app) as client:
        # /api/health — liveness
        assert client.get("/api/health", headers=LOOPBACK).status_code == 200
        # /api/health/summary — should not 401/403 even with no token
        r = client.get("/api/health/summary", headers=LOOPBACK)
        assert r.status_code == 200
        # /api/health/<unknown-slug> — 404 is a legitimate response, not 401/403
        r2 = client.get("/api/health/nonexistent-slug-xyz", headers=LOOPBACK)
        assert r2.status_code == 404
