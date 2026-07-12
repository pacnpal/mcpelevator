"""Servers API tests — the parts of the control-plane CRUD worth exercising over HTTP.

Focus: the docker runner's opt-in gate must surface as a clean 400 (not an uncaught 500)
when a disabled docker server is enabled while the root-equivalent runner is still off.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

LOOPBACK = {"host": "127.0.0.1"}


def test_enable_docker_server_gated_returns_400():
    with TestClient(app) as c:
        # Ensure the runner is off (default), then import a docker server (created disabled).
        c.patch("/api/settings", json={"docker_runner": False}, headers=LOOPBACK)
        imported = c.post(
            "/api/servers/import",
            json={
                "mcpServers": {
                    "gh": {
                        "command": "docker",
                        "args": ["run", "--rm", "-e", "T", "img:1"],
                        "env": {"T": "v"},
                    }
                }
            },
            headers=LOOPBACK,
        )
        assert imported.status_code == 201, imported.text
        server_id = imported.json()["created"][0]["id"]

        # Enabling while docker is off must be a 400 with a useful message — not a 500.
        resp = c.post(f"/api/servers/{server_id}/enable", headers=LOOPBACK)
        assert resp.status_code == 400, resp.text
        assert "disabled" in resp.json()["detail"].lower()

        # Turning the runner on lets the same enable succeed.
        c.patch("/api/settings", json={"docker_runner": True}, headers=LOOPBACK)
        try:
            ok = c.post(f"/api/servers/{server_id}/enable", headers=LOOPBACK)
            assert ok.status_code == 200, ok.text
            assert ok.json()["enabled"] is True
        finally:
            # Leave global state clean for other tests sharing the app/engine.
            c.post(f"/api/servers/{server_id}/disable", headers=LOOPBACK)
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)
            c.patch("/api/settings", json={"docker_runner": False}, headers=LOOPBACK)
