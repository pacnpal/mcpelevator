"""Idle quiescence (auto-stop) + wake-on-request tests.

Supervisor-level: the reconcile sweep stops a running unit whose idle window has
passed, keeps it down while quiesced, and restarts it on a wake/activation
request. API-level: the "idle" state surfaces as-is (with cached tools), the
idle_timeout_s knobs validate, and the proxy holds a request through a wake.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from conftest import LOOPBACK, create_server

from app.db import get_engine, init_db, repo
from app.db.models import utcnow
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.supervisor.supervisor import Supervisor

init_db()


class _StoppableUnit(SimpleNamespace):
    async def stop(self):
        self.state = "stopped"


def _running_unit(server) -> _StoppableUnit:
    return _StoppableUnit(
        slug=server.slug,
        config_hash=server.config_hash,
        state="running",
        pid=4321,
        port=49321,
        last_error=None,
        tools=[{"name": "cached", "description": "", "input_schema": {}}],
        restart_count=0,
        last_health=None,
        startup_status=None,
    )


def _make_server(**kwargs):
    with Session(get_engine()) as session:
        return service.create_server(session, **kwargs)


def _cleanup(sid: str) -> None:
    with Session(get_engine()) as session:
        repo.delete_server(session, sid)


async def test_idle_sweep_quiesces_inactive_running_unit(monkeypatch):
    server = _make_server(
        name="Idler", runner="npx", command="npx", enabled=True, idle_timeout_s=30
    )
    sid = server.id
    sup = Supervisor()
    unit = _running_unit(server)
    sup.units[sid] = unit
    sup._last_activity[sid] = utcnow() - timedelta(seconds=31)
    started: list[str] = []

    async def fake_start(sv, *, activation_started_at=None):
        started.append(sv.id)
        return None

    monkeypatch.setattr(sup, "_try_start", fake_start)
    try:
        await sup.reconcile_once()
        assert sid not in sup.units          # bridge stopped
        assert sup.is_idle(sid)
        assert started == []                 # ...and NOT restarted the same pass
        with Session(get_engine()) as session:
            rt = repo.get_runtime(session, sid)
        assert rt is not None and rt.state == "idle"
        assert rt.pid is None and rt.port is None and rt.last_error is None
        assert rt.tools == unit.tools        # cached tool list survives quiescence

        # A later pass leaves the quiesced server down (desired-but-idle).
        await sup.reconcile_once()
        assert started == []
        assert sup.is_idle(sid)
    finally:
        sup.units.pop(sid, None)
        _cleanup(sid)


async def test_recent_activity_prevents_idling():
    server = _make_server(
        name="Busy", runner="npx", command="npx", enabled=True, idle_timeout_s=3600
    )
    sid = server.id
    sup = Supervisor()
    sup.units[sid] = _running_unit(server)
    sup.mark_activity(sid)
    try:
        await sup.reconcile_once()
        assert sid in sup.units and sup.units[sid].state == "running"
        assert not sup.is_idle(sid)
    finally:
        sup.units.pop(sid, None)
        _cleanup(sid)


async def test_zero_timeout_pins_server_always_on(monkeypatch):
    """A per-server idle_timeout_s of 0 must override a nonzero global default."""
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"idle_timeout_s": 5})
    server = _make_server(
        name="Pinned", runner="npx", command="npx", enabled=True, idle_timeout_s=0
    )
    sid = server.id
    sup = Supervisor()
    sup.units[sid] = _running_unit(server)
    sup._last_activity[sid] = utcnow() - timedelta(hours=1)
    try:
        await sup.reconcile_once()
        assert sid in sup.units and not sup.is_idle(sid)
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            runtime_settings.write(session, {"idle_timeout_s": 0})
        _cleanup(sid)


async def test_global_default_applies_when_server_inherits(monkeypatch):
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"idle_timeout_s": 30})
    server = _make_server(name="Inherit", runner="npx", command="npx", enabled=True)
    sid = server.id
    sup = Supervisor()
    sup.units[sid] = _running_unit(server)
    sup._last_activity[sid] = utcnow() - timedelta(seconds=31)
    try:
        await sup.reconcile_once()
        assert sup.is_idle(sid)
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            runtime_settings.write(session, {"idle_timeout_s": 0})
        _cleanup(sid)


async def test_in_flight_stream_prevents_idling():
    """An open proxied response stream (e.g. a long-lived SSE session) must keep
    the bridge alive even when no NEW request has arrived inside the window."""
    server = _make_server(
        name="Streamer", runner="npx", command="npx", enabled=True, idle_timeout_s=30
    )
    sid = server.id
    sup = Supervisor()
    sup.units[sid] = _running_unit(server)
    sup._last_activity[sid] = utcnow() - timedelta(hours=1)
    sup.request_started(sid)
    try:
        await sup.reconcile_once()
        assert sid in sup.units and not sup.is_idle(sid)
        # Stream closes: the idle clock restarts from now, so the next pass
        # still doesn't quiesce (fresh activity), but the server is sweepable again.
        sup.request_finished(sid)
        await sup.reconcile_once()
        assert sid in sup.units and not sup.is_idle(sid)
    finally:
        sup.units.pop(sid, None)
        _cleanup(sid)


async def test_disabling_idle_timeout_resumes_quiesced_server(monkeypatch):
    """A server already idle when its effective timeout drops to 0 (per-server
    edit or global-default change) must resume — 'always running' means running."""
    server = _make_server(
        name="Resumer", runner="npx", command="npx", enabled=True, idle_timeout_s=30
    )
    sid = server.id
    sup = Supervisor()
    sup._idle.add(sid)
    started: list[str] = []

    async def fake_start(sv, *, activation_started_at=None):
        started.append(sv.id)
        sup.units[sv.id] = _running_unit(sv)
        return None

    monkeypatch.setattr(sup, "_try_start", fake_start)
    try:
        # Still quiesced while the timeout stands.
        await sup.reconcile_once()
        assert started == [] and sup.is_idle(sid)
        # The operator pins the server always-on; reconcile alone must resume it.
        with Session(get_engine()) as session:
            service.update_server(session, sid, {"idle_timeout_s": 0})
        await sup.reconcile_once()
        assert started == [sid]
        assert not sup.is_idle(sid)
    finally:
        sup.units.pop(sid, None)
        _cleanup(sid)


async def test_wake_restarts_quiesced_server(monkeypatch):
    server = _make_server(
        name="Waker", runner="npx", command="npx", enabled=True, idle_timeout_s=30
    )
    sid = server.id
    sup = Supervisor()
    sup._idle.add(sid)
    started: list[str] = []

    async def fake_start(sv, *, activation_started_at=None):
        started.append(sv.id)
        sup.units[sv.id] = _running_unit(sv)
        return None

    monkeypatch.setattr(sup, "_try_start", fake_start)
    try:
        assert sup.wake(sid) is True
        await sup.reconcile_once()
        assert started == [sid]
        assert not sup.is_idle(sid)
        # wake() is itself activity, so the fresh unit isn't instantly re-idled
        assert sid in sup.units
    finally:
        sup.units.pop(sid, None)
        _cleanup(sid)


async def test_wake_is_noop_for_non_idle_server():
    sup = Supervisor()
    assert sup.wake("nonexistent") is False
    assert sup.activation_requested_at("nonexistent") is None


async def test_disable_while_idle_converges_to_stopped():
    server = _make_server(
        name="IdleOff", runner="npx", command="npx", enabled=True, idle_timeout_s=30
    )
    sid = server.id
    sup = Supervisor()
    sup._idle.add(sid)
    with Session(get_engine()) as session:
        repo.upsert_runtime(session, sid, state="idle", pid=None, port=None,
                            last_error=None, restart_count=0, last_health=None,
                            tools=[{"name": "cached"}])
        service.set_enabled(session, sid, False)
    try:
        await sup.reconcile_once()
        assert not sup.is_idle(sid)  # marker pruned with the desired set
        with Session(get_engine()) as session:
            rt = repo.get_runtime(session, sid)
        assert rt is not None and rt.state == "stopped" and rt.tools == []
    finally:
        _cleanup(sid)


# --- API surface -------------------------------------------------------------- #


def test_api_surfaces_idle_state_with_cached_tools():
    with TestClient(app) as client:
        srv = create_server(client, name="idle-api")
        sid = srv["id"]
        try:
            # Flip desired state directly (not via /enable, which queues a REAL
            # activation and would surface as "starting" until it runs).
            with Session(get_engine()) as session:
                row = repo.get_server(session, sid)
                row.enabled = True
                repo.save_server(session, row)
            client.app.state.supervisor._idle.add(sid)
            with Session(get_engine()) as session:
                repo.upsert_runtime(
                    session, sid, state="idle", pid=None, port=None, last_error=None,
                    restart_count=0, last_health=None,
                    tools=[{"name": "t1", "description": "", "input_schema": {}}],
                )
            detail = client.get(f"/api/servers/{sid}", headers=LOOPBACK).json()
            assert detail["state"] == "idle"
            assert detail["tools_count"] == 1
            assert detail["tools"][0]["name"] == "t1"
            # idle is wakeable, so per-server health passes instead of 503ing
            h = client.get(f"/api/health/{srv['slug']}", headers=LOOPBACK)
            assert h.status_code == 200
            assert h.json()["status"] == "idle"
        finally:
            client.app.state.supervisor._idle.discard(sid)
            client.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_idle_timeout_validation_on_server_and_settings():
    with TestClient(app) as client:
        # server-level: negative rejected, positive accepted, null = inherit
        r = client.post(
            "/api/servers",
            json={"name": "bad-idle", "command": "echo", "idle_timeout_s": -5},
            headers=LOOPBACK,
        )
        assert r.status_code == 400
        srv = client.post(
            "/api/servers",
            json={"name": "good-idle", "command": "echo", "idle_timeout_s": 600},
            headers=LOOPBACK,
        ).json()
        try:
            detail = client.get(f"/api/servers/{srv['id']}", headers=LOOPBACK).json()
            assert detail["idle_timeout_s"] == 600
            # explicit null clears the override back to "inherit"
            r = client.patch(
                f"/api/servers/{srv['id']}", json={"idle_timeout_s": None}, headers=LOOPBACK
            )
            assert r.status_code == 200
            detail = client.get(f"/api/servers/{srv['id']}", headers=LOOPBACK).json()
            assert detail["idle_timeout_s"] is None

            # A JSON boolean must be rejected at the API boundary (StrictInt) —
            # lax coercion would silently turn `true` into a 1-second shutdown.
            assert (
                client.patch(
                    f"/api/servers/{srv['id']}", json={"idle_timeout_s": True}, headers=LOOPBACK
                ).status_code
                == 422
            )

            # settings-level: invalid rejected, valid persisted (and restored)
            assert (
                client.patch(
                    "/api/settings", json={"idle_timeout_s": -1}, headers=LOOPBACK
                ).status_code
                == 400
            )
            assert (
                client.patch(
                    "/api/settings", json={"idle_timeout_s": True}, headers=LOOPBACK
                ).status_code
                == 422
            )
            updated = client.patch(
                "/api/settings", json={"idle_timeout_s": 900}, headers=LOOPBACK
            )
            assert updated.status_code == 200
            assert updated.json()["idle_timeout_s"] == 900
            client.patch("/api/settings", json={"idle_timeout_s": 0}, headers=LOOPBACK)
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_group_request_counts_members_in_flight():
    """A /g request holds every member's in-flight count for the whole delegation,
    so a long-lived group stream can't have a member bridge idled out mid-session."""
    with TestClient(app) as client:
        srv = create_server(client, name="grp-member", auth="none")
        sid = srv["id"]
        group = "inflight-grp"
        try:
            r = client.put(f"/api/groups/{group}", json={"members": [sid]}, headers=LOOPBACK)
            assert r.status_code == 200, r.text
            sup = client.app.state.supervisor
            observed: dict = {}

            async def inner(scope, receive, send):
                observed["during"] = sup._in_flight.get(sid, 0)
                from starlette.responses import Response

                await Response("ok")(scope, receive, send)

            client.app.state.groups.app_for = lambda name: inner
            r = client.get(f"/g/{group}/mcp", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            assert observed["during"] == 1          # held while the inner app ran
            assert sup._in_flight.get(sid, 0) == 0  # released afterwards
        finally:
            client.delete(f"/api/groups/{group}", headers=LOOPBACK)
            client.delete(f"/api/servers/{sid}", headers=LOOPBACK)


# --- proxy wake-on-request ---------------------------------------------------- #


async def _upstream_ok(request):
    return JSONResponse({"ok": True})


_upstream = Starlette(routes=[Route("/{path:path}", _upstream_ok, methods=["GET", "POST"])])


def test_proxy_wakes_idle_server_and_serves_request():
    with TestClient(app) as client:
        srv = create_server(client, name="wake-proxy", auth="none")
        sid = srv["id"]
        try:
            client.app.state.http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_upstream)
            )
            sup = client.app.state.supervisor
            woken: list[str] = []
            state = {"up": False}

            def fake_wake(server_id):
                woken.append(server_id)
                state["up"] = True  # the "activation" comes up immediately
                return True

            sup.wake = fake_wake
            sup.endpoint = lambda slug: ("backend", 9000) if state["up"] else None

            r = client.get(f"/s/{srv['slug']}/mcp", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            assert woken == [sid]
        finally:
            client.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_proxy_503_when_wake_declined():
    """A stopped (not idle) server still 503s — the wake path only covers idle."""
    with TestClient(app) as client:
        srv = create_server(client, name="no-wake", auth="none")
        try:
            sup = client.app.state.supervisor
            sup.endpoint = lambda slug: None
            r = client.get(f"/s/{srv['slug']}/mcp", headers=LOOPBACK)
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "5"
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)
