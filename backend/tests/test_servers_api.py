"""Servers API tests — the parts of the control-plane CRUD worth exercising over HTTP.

Focus: the docker runner's opt-in gate must surface as a clean 400 (not an uncaught 500)
when a disabled docker server is enabled while the root-equivalent runner is still off.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine, repo
from app.main import app
from app.registry import service
from app.supervisor.supervisor import Supervisor
from app.supervisor.unit import ServerUnit

LOOPBACK = {"host": "127.0.0.1"}


def test_setup_script_api_round_trip_and_runner_validation():
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Prepared",
                "runner": "command",
                "command": "/bin/true",
                "setup_script": "printf 'ready\\n'\n",
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.status_code == 200
            assert detail.json()["setup_script"] == "printf 'ready\\n'\n"

            rejected = c.patch(
                f"/api/servers/{server_id}",
                json={"runner": "remote", "command": "https://up.example/mcp"},
                headers=LOOPBACK,
            )
            assert rejected.status_code == 400
            assert "local runners" in rejected.json()["detail"]
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_enabled_create_returns_queued_without_stale_runtime(monkeypatch):
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Queued",
                "runner": "command",
                "command": "/bin/true",
                "enabled": True,
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        try:
            assert body["state"] == "starting"
            assert body["startup_status"]["phase"] == "queued"
            assert body["startup_status"]["attempt"] == 1
            assert body["pid"] is None
            assert body["port"] is None
        finally:
            c.delete(f"/api/servers/{body['id']}", headers=LOOPBACK)


def test_retry_starts_fresh_activation_without_changing_config(monkeypatch):
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Retry", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            with Session(get_engine()) as session:
                server = service.set_enabled(session, server_id, True)
                before = (server.config_hash, server.updated_at)
                repo.upsert_runtime(
                    session,
                    server_id,
                    state="failed",
                    pid=None,
                    port=None,
                    last_error="setup exited with code 7",
                    tools=[],
                )

            retried = c.post(f"/api/servers/{server_id}/retry", headers=LOOPBACK)
            assert retried.status_code == 200, retried.text
            body = retried.json()
            assert body["state"] == "starting"
            assert body["startup_status"]["phase"] == "queued"
            assert body["last_error"] is None

            with Session(get_engine()) as session:
                current = repo.get_server(session, server_id)
                assert current is not None
                assert (current.config_hash, current.updated_at) == before
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_launch_edit_returns_queued_instead_of_old_running_unit(monkeypatch):
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Edit", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            with Session(get_engine()) as session:
                server = service.set_enabled(session, server_id, True)
                old_unit = ServerUnit(server)
                old_unit.state = "running"
                old_unit.port = 49999
                c.app.state.supervisor.units[server_id] = old_unit

            edited = c.patch(
                f"/api/servers/{server_id}",
                json={"setup_script": "printf 'new setup\\n'\n"},
                headers=LOOPBACK,
            )
            assert edited.status_code == 200, edited.text
            body = edited.json()
            assert body["state"] == "starting"
            assert body["startup_status"]["phase"] == "queued"
            assert body["port"] is None
            assert body["tools_count"] == 0
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_disable_returns_stopping_instead_of_stale_running_runtime(monkeypatch):
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Stop", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            with Session(get_engine()) as session:
                service.set_enabled(session, server_id, True)
                repo.upsert_runtime(
                    session,
                    server_id,
                    state="running",
                    pid=123,
                    port=49999,
                    last_error=None,
                    tools=[],
                )

            stopped = c.post(f"/api/servers/{server_id}/disable", headers=LOOPBACK)
            assert stopped.status_code == 200, stopped.text
            body = stopped.json()
            assert body["state"] == "stopping"
            assert body["pid"] is None
            assert body["port"] is None
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_docker_run_args_api_round_trip_and_validation():
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "gh",
                "runner": "docker",
                "command": "img:1",
                "args": ["serve"],
                "run_args": ["--name", "my-mcp", "--shm-size=1g"],
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.status_code == 200
            assert detail.json()["run_args"] == ["--name", "my-mcp", "--shm-size=1g"]

            patched = c.patch(
                f"/api/servers/{server_id}",
                json={"run_args": ["--shm-size=2g"]},
                headers=LOOPBACK,
            )
            assert patched.status_code == 200, patched.text
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.json()["run_args"] == ["--shm-size=2g"]

            # A forbidden run option is a clean 400 with the reason, not a 500.
            rejected = c.patch(
                f"/api/servers/{server_id}",
                json={"run_args": ["-e", "SECRET=x"]},
                headers=LOOPBACK,
            )
            assert rejected.status_code == 400
            assert "Environment" in rejected.json()["detail"]
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


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

        try:
            # Enabling while docker is off must be a 400 with a useful message — not a 500.
            # (The docker-on enable path is covered at the service level in test_registry;
            # we avoid it here so the reconciler never attempts a real container spawn.)
            resp = c.post(f"/api/servers/{server_id}/enable", headers=LOOPBACK)
            assert resp.status_code == 400, resp.text
            assert "disabled" in resp.json()["detail"].lower()
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)
