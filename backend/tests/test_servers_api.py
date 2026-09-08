"""Servers API tests — the parts of the control-plane CRUD worth exercising over HTTP.

Focus: the docker runner's opt-in gate must surface as a clean 400 (not an uncaught 500)
when a disabled docker server is enabled while the root-equivalent runner is still off.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthToken
from sqlmodel import Session

from conftest import LOOPBACK

from app.auth.oauth_store import ServerTokenStorage
from app.db import get_engine, repo
from app.main import app
from app.registry import service
from app.supervisor.supervisor import Supervisor
from app.supervisor.unit import ServerUnit



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


def test_disabled_tools_api_round_trip():
    """disabled_tools is accepted on create, normalized (deduped/sorted — names kept
    exactly), echoed on GET, and replaceable via PATCH ([] re-exposes everything)."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Hidden",
                "runner": "command",
                "command": "/bin/true",
                # " a_tool " is a DIFFERENT tool from "a_tool": the name is the upstream's
                # identity, so it has to cross the HTTP boundary untouched or the hide
                # would be written against a tool that doesn't exist.
                "disabled_tools": ["z_tool", "a_tool", "a_tool", " a_tool "],
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.status_code == 200
            assert detail.json()["disabled_tools"] == [" a_tool ", "a_tool", "z_tool"]

            patched = c.patch(
                f"/api/servers/{server_id}",
                json={"disabled_tools": []},
                headers=LOOPBACK,
            )
            assert patched.status_code == 200, patched.text
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.json()["disabled_tools"] == []
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_normalize_schema_dialect_api_round_trip():
    """normalize_schema_dialect defaults false, is accepted on create, echoed on GET,
    and PATCH-able independent of runner (issue #123)."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Draft07", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.json()["normalize_schema_dialect"] is False

            patched = c.patch(
                f"/api/servers/{server_id}",
                json={"normalize_schema_dialect": True},
                headers=LOOPBACK,
            )
            assert patched.status_code == 200, patched.text
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.json()["normalize_schema_dialect"] is True
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_tool_overrides_api_round_trip():
    """tool_overrides is accepted on create, normalized, echoed on GET, replaceable via
    PATCH ({} restores the upstream labels), and an unusable rename is a 400."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Relabelled",
                "runner": "command",
                "command": "/bin/true",
                "tool_overrides": {
                    "do_thing": {"name": "run_report", "description": " Runs it. "},
                    "other": {"description": ""},
                },
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.status_code == 200
            # "other" had nothing left after trimming, so it isn't stored at all.
            assert detail.json()["tool_overrides"] == {
                "do_thing": {"name": "run_report", "description": "Runs it."}
            }

            rejected = c.patch(
                f"/api/servers/{server_id}",
                json={"tool_overrides": {"do_thing": {"name": "not a valid name"}}},
                headers=LOOPBACK,
            )
            assert rejected.status_code == 400, rejected.text

            # A typo'd member must NOT be silently dropped. This has to be asserted at
            # the HTTP boundary: pydantic strips unknown fields before the service
            # normalizer runs, so the normalizer's own check can't cover API traffic.
            typo = c.patch(
                f"/api/servers/{server_id}",
                json={"tool_overrides": {"do_thing": {"desc": "typo"}}},
                headers=LOOPBACK,
            )
            assert typo.status_code == 422, typo.text
            assert "desc" in typo.text

            patched = c.patch(
                f"/api/servers/{server_id}",
                json={"tool_overrides": {}},
                headers=LOOPBACK,
            )
            assert patched.status_code == 200, patched.text
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.json()["tool_overrides"] == {}
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_tool_overrides_echo_only_the_fields_that_are_set():
    """An override of ONE field must not come back carrying an explicit null for the other.

    Storage is sparse (the normalizer drops blank/absent fields), so materializing the
    missing field on the way out would describe a shape that was never stored, and one the
    SPA's `name?: string` contract doesn't have — an absent name would read as the string
    `null` rather than "keep the upstream's"."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Sparse",
                "runner": "command",
                "command": "/bin/true",
                "tool_overrides": {
                    "renamed": {"name": "run_report"},
                    "redescribed": {"description": "Runs it."},
                },
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK)
            assert detail.json()["tool_overrides"] == {
                "renamed": {"name": "run_report"},
                "redescribed": {"description": "Runs it."},
            }
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


def test_restart_bounces_the_unit_and_requeues_without_changing_config(monkeypatch):
    """Restart stops the live unit and queues a fresh activation. It is desired-state
    neutral: config_hash/updated_at are untouched (so it isn't an edit), and the
    response reads as a queued start."""
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Restart", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            sup = c.app.state.supervisor
            with Session(get_engine()) as session:
                server = service.set_enabled(session, server_id, True)
                before = (server.config_hash, server.updated_at)
                unit = ServerUnit(server)
                unit.state = "running"
                unit.port = 49998
                sup.units[server_id] = unit

            restarted = c.post(f"/api/servers/{server_id}/restart", headers=LOOPBACK)
            assert restarted.status_code == 200, restarted.text
            body = restarted.json()
            assert body["state"] == "starting"
            assert body["startup_status"]["phase"] == "queued"
            # The running unit is gone and an activation is queued for the reconciler.
            assert server_id not in sup.units
            assert sup.activation_requested_at(server_id) is not None

            with Session(get_engine()) as session:
                current = repo.get_server(session, server_id)
                assert current is not None
                assert (current.config_hash, current.updated_at) == before
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_restart_wakes_an_idle_server(monkeypatch):
    """An idle server is desired-but-quiesced. Restarting clears the marker and queues
    an activation, so the operator's button works from `idle` exactly like from
    `running` — no need to send traffic first."""
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Idle restart", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            sup = c.app.state.supervisor
            with Session(get_engine()) as session:
                service.set_enabled(session, server_id, True)
                repo.upsert_runtime(session, server_id, state="idle", tools=[])
            sup._idle.add(server_id)

            restarted = c.post(f"/api/servers/{server_id}/restart", headers=LOOPBACK)
            assert restarted.status_code == 200, restarted.text
            assert restarted.json()["state"] == "starting"
            assert not sup.is_idle(server_id)
            assert sup.activation_requested_at(server_id) is not None
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_restart_rejects_a_disabled_server(monkeypatch):
    """A disabled server has no bridge to bounce — Start is the action for it."""
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Off", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            r = c.post(f"/api/servers/{server_id}/restart", headers=LOOPBACK)
            assert r.status_code == 409
            assert "restarted" in r.json()["detail"]
            assert c.app.state.supervisor.activation_requested_at(server_id) is None
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_restart_404s_for_an_unknown_server():
    with TestClient(app) as c:
        assert c.post("/api/servers/ghost/restart", headers=LOOPBACK).status_code == 404


def test_restart_refuses_when_a_disable_lands_during_teardown(monkeypatch):
    """The endpoint promises to bounce only a DESIRED server, but stopping the unit
    awaits process teardown — long enough for a disable to commit. The authorization
    hook re-reads the committed row at the supervisor's decision points, so the stop
    stands (that IS the new desired state) and no fresh activation is queued."""
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Raced", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        try:
            sup = c.app.state.supervisor
            with Session(get_engine()) as session:
                server = service.set_enabled(session, server_id, True)
                unit = ServerUnit(server)
                unit.state = "running"
                unit.port = 49997
                sup.units[server_id] = unit

            async def disabling_stop():
                # Stands in for a slow teardown that a concurrent disable commits during.
                with Session(get_engine()) as session:
                    service.set_enabled(session, server_id, False)

            monkeypatch.setattr(unit, "stop", disabling_stop)

            r = c.post(f"/api/servers/{server_id}/restart", headers=LOOPBACK)
            assert r.status_code == 409, r.text
            assert "restarted" in r.json()["detail"]
            assert server_id not in sup.units  # the stop stands: it's the desired state
            assert sup.activation_requested_at(server_id) is None
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_delete_stops_a_server_relaunched_during_the_delete(monkeypatch):
    """The delete's stop runs while the row still exists, so a reconcile pass can consume
    a queued activation (an operator restart) in the gap and relaunch the server before
    the row is removed. The delete cancels and stops again afterwards, so nothing is left
    running for a server that no longer exists."""
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Raced delete", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]
        sup = c.app.state.supervisor
        with Session(get_engine()) as session:
            server = service.set_enabled(session, server_id, True)

        # Stand in for the reconciler winning the gap: the first stop (before the row is
        # removed) is followed by a relaunch, exactly as a consumed activation would.
        relaunched = ServerUnit(server)
        relaunched.state = "running"
        relaunched.port = 49996
        real_stop = Supervisor._stop
        stops: list[str] = []

        async def relaunching_stop(self, sid):
            await real_stop(self, sid)
            stops.append(sid)
            if len(stops) == 1:
                self.units[sid] = relaunched  # the reconciler's replacement

        monkeypatch.setattr(Supervisor, "_stop", relaunching_stop)
        sup.request_activation(server_id)

        assert c.delete(f"/api/servers/{server_id}", headers=LOOPBACK).status_code == 204
        assert server_id not in sup.units  # the relaunch was torn down too
        assert sup.activation_requested_at(server_id) is None


def test_delete_cleans_up_even_when_the_final_teardown_fails(monkeypatch):
    """The row is already gone by the second stop, so a teardown that raises there can't
    be retried through this endpoint — a second DELETE 404s at its lookup. The credential
    file and the group remount only ever happen here, so they must happen either way; the
    process itself is the supervisor's problem (it keeps the unit and retries the stop)."""
    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Wedged delete", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        ).json()
        server_id = created["id"]

        store = ServerTokenStorage(server_id)
        asyncio.run(store.set_tokens(OAuthToken(access_token="AT", token_type="Bearer")))
        assert store.path.exists()

        stops: list[str] = []
        real_stop = Supervisor.stop

        async def wedged_second_stop(self, sid):
            stops.append(sid)
            if len(stops) == 2:  # the one after the row is gone
                raise RuntimeError("docker daemon is wedged")
            await real_stop(self, sid)

        monkeypatch.setattr(Supervisor, "stop", wedged_second_stop)

        with pytest.raises(RuntimeError):
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)

        assert len(stops) == 2
        assert not store.path.exists()  # no orphan credential file for a deleted server
