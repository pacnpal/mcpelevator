"""Supervisor reconcile tests — the parts that don't spawn real bridge processes.

Focus: a slug rename must converge onto a running unit's in-memory routing key.
slug is excluded from ``config_hash`` (a rename must not bounce the bridge), so the
reconciler — not a restart — is what keeps a live unit's ``slug`` in sync with
desired state. This guards the race where ``rename_slug`` missed a unit that didn't
exist yet and was then started from a pre-rename snapshot.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlmodel import Session

from mcp.types import Tool

from app.db import get_engine, init_db, repo
from app.registry import service
from app.registry import settings as runtime_settings
from app.supervisor.supervisor import Supervisor
from app.supervisor.unit import tool_summary

init_db()  # ensure the global-engine tables exist when this module runs alone


def test_tool_summary_records_output_schema_presence():
    """The probe's cached entry must tell the UI whether a tool declares an
    outputSchema (the signal behind clients' "recommended: add one" hint)."""
    with_schema = Tool(
        name="structured",
        description="d",
        inputSchema={"type": "object"},
        outputSchema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    without_schema = Tool(name="bare", inputSchema={"type": "object"})

    assert tool_summary(with_schema) == {
        "name": "structured",
        "description": "d",
        "has_output_schema": True,
    }
    assert tool_summary(without_schema) == {
        "name": "bare",
        "description": "",
        "has_output_schema": False,
    }


def _fake_unit(server) -> SimpleNamespace:
    """A stand-in for a live ServerUnit carrying only what reconcile reads/writes."""
    return SimpleNamespace(
        slug=server.slug,
        config_hash=server.config_hash,
        state="running",
        pid=1234,
        port=9999,
        last_error=None,
        tools=[],
        restart_count=0,
        last_health=None,
        startup_status=None,
    )


def _force_enable_legacy(session, **kwargs) -> str:
    """Persist an enabled row that bypasses service validation, standing in for a legacy row
    (or a hand-edited DB) that predates the shell-wrapped-docker guard."""
    server = service.create_server(session, enabled=False, **kwargs)
    row = repo.get_server(session, server.id)
    row.enabled = True
    repo.save_server(session, row)
    return server.id


async def test_reconcile_forbids_shell_wrapped_docker_when_runner_disabled():
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"docker_runner": False})
        sid = _force_enable_legacy(
            session,
            name="wrapped-off",
            runner="command",
            command="/bin/sh",
            args=["-c", "docker run --privileged alpine"],
            env={},
        )

    sup = Supervisor()
    # Seed a running unit (as if the row had been started before the gate applied) so reconcile
    # exercises the stop-and-fail path, not just the never-started path.
    stopped: list[str] = []

    class _StoppableUnit(SimpleNamespace):
        async def stop(self):
            stopped.append(sid)

    sup.units[sid] = _StoppableUnit(slug="wrapped-off", config_hash="x", state="running",
                                    pid=1, port=1, last_error=None, tools=[])
    sup.request_activation(sid)
    try:
        await sup.reconcile_once()
        assert stopped == [sid]                       # the running unit was stopped
        assert sid not in sup.units                   # ...and removed
        assert sup.activation_requested_at(sid) is None  # its activation request was cancelled
        with Session(get_engine()) as session:
            rt = repo.get_runtime(session, sid)
        assert rt is not None
        assert rt.state == "failed"
        assert "docker runner" in (rt.last_error or "").lower()
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            repo.delete_server(session, sid)


async def test_reconcile_forbids_shell_wrapped_docker_even_when_runner_enabled():
    """A shell-wrapped ``docker`` CLI on a passthrough runner can't be hardened, so it must be
    refused even while ``docker_runner`` is on — otherwise reconcile would start it through the
    passthrough command runner with the full control-plane environment."""
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"docker_runner": True})
        sid = _force_enable_legacy(
            session,
            name="wrapped-on",
            runner="command",
            command="/bin/sh",
            args=["-c", "docker run --privileged alpine"],
            env={},
        )

    sup = Supervisor()
    try:
        await sup.reconcile_once()
        assert sid not in sup.units  # never started, despite the runner being on
        with Session(get_engine()) as session:
            rt = repo.get_runtime(session, sid)
        assert rt is not None
        assert rt.state == "failed"
        assert "docker runner" in (rt.last_error or "").lower()
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            runtime_settings.write(session, {"docker_runner": False})
            repo.delete_server(session, sid)


async def test_reconcile_forbids_setup_script_docker_when_runner_enabled():
    """A benign command with a ``setup_script`` that invokes docker runs that script as
    ``/bin/sh -e -c`` with the passthrough env, so reconcile must refuse it even while
    ``docker_runner`` is on — exercising the setup-script half of the reconciliation guard."""
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"docker_runner": True})
        sid = _force_enable_legacy(
            session,
            name="setup-docker",
            runner="command",
            command="echo",
            args=["hi"],
            env={},
            setup_script="docker run --privileged alpine",
        )

    sup = Supervisor()
    try:
        await sup.reconcile_once()
        assert sid not in sup.units  # never started, despite the runner being on
        with Session(get_engine()) as session:
            rt = repo.get_runtime(session, sid)
        assert rt is not None
        assert rt.state == "failed"
        assert "docker runner" in (rt.last_error or "").lower()
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            runtime_settings.write(session, {"docker_runner": False})
            repo.delete_server(session, sid)


async def test_reconcile_skips_docker_when_runner_disabled():
    """An enabled docker server must not be started while the docker runner is off (e.g.
    the setting was turned off after it was enabled). Reconcile leaves no unit and records
    a clear failed state — never a silent spawn of a root-equivalent container."""
    sup = Supervisor()
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"docker_runner": True})
        server = service.create_server(
            session, name="Dk", runner="docker", command="img:1", args=[], env={}, enabled=True
        )
        sid = server.id
        # Now turn the runner off — the enabled docker row must be refused, not started.
        runtime_settings.write(session, {"docker_runner": False})
    try:
        await sup.reconcile_once()
        assert sid not in sup.units  # never started
        with Session(get_engine()) as session:
            rt = repo.get_runtime(session, sid)
        assert rt is not None
        assert rt.state == "failed"
        assert "disabled" in (rt.last_error or "")
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            runtime_settings.write(session, {"docker_runner": False})
            repo.delete_server(session, sid)


async def test_reconcile_clears_queued_runtime_when_server_is_disabled():
    sup = Supervisor()
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Queued", runner="npx", command="npx", enabled=False
        )
        sid = server.id
        repo.upsert_runtime(
            session,
            sid,
            state="queued",
            pid=1234,
            port=9999,
            last_error="stale",
            restart_count=2,
            tools=[{"name": "stale"}],
        )
    sup.request_activation(sid)
    try:
        await sup.reconcile_once()

        assert sup.activation_requested_at(sid) is None
        with Session(get_engine()) as session:
            runtime = repo.get_runtime(session, sid)
        assert runtime is not None
        assert runtime.state == "stopped"
        assert runtime.pid is None
        assert runtime.port is None
        assert runtime.last_error is None
        assert runtime.restart_count == 0
        assert runtime.tools == []
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, sid)


async def test_reconcile_converges_renamed_slug_onto_live_unit():
    sup = Supervisor()
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Conv", runner="npx", command="npx", args=["-y", "x"], enabled=True
        )
    sid = server.id
    try:
        # A live unit pinned to the OLD slug (as if rename_slug missed it).
        unit = _fake_unit(server)
        unit.slug = "stale-slug"
        sup.units[sid] = unit

        # Desired state now carries the renamed slug.
        with Session(get_engine()) as session:
            service.update_server(session, sid, {"slug": "fresh-slug"})

        await sup.reconcile_once()

        # The reconciler copied the fresh slug onto the live unit (no restart:
        # same config_hash means the unit object is unchanged, only its slug).
        assert sup.units[sid] is unit
        assert unit.slug == "fresh-slug"
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            repo.delete_server(session, sid)


async def test_reconcile_replaces_unhealthy_but_keeps_failed_terminal(monkeypatch):
    sup = Supervisor()
    with Session(get_engine()) as session:
        unhealthy_server = service.create_server(
            session, name="Unhealthy", runner="npx", command="npx", enabled=True
        )
        failed_server = service.create_server(
            session, name="Failed", runner="npx", command="npx", enabled=True
        )
        session.refresh(unhealthy_server)
        unhealthy_data = vars(_fake_unit(unhealthy_server))
        failed = _fake_unit(failed_server)
        unhealthy_id = unhealthy_server.id
        failed_id = failed_server.id

    class StoppableUnit(SimpleNamespace):
        async def stop(self):
            self.state = "stopped"

    unhealthy = StoppableUnit(**unhealthy_data)
    unhealthy.state = "unhealthy"
    failed.state = "failed"
    sup.units[unhealthy_id] = unhealthy
    sup.units[failed_id] = failed
    restarted: list[str] = []

    async def fake_start(server, *, activation_started_at=None):
        restarted.append(server.id)
        sup.units[server.id] = _fake_unit(server)
        return None

    monkeypatch.setattr(sup, "_try_start", fake_start)
    try:
        await sup.reconcile_once()
        assert restarted == [unhealthy_id]
        assert sup.units[unhealthy_id] is not unhealthy
        assert sup.units[failed_id] is failed
    finally:
        sup.units.clear()
        with Session(get_engine()) as session:
            repo.delete_server(session, unhealthy_id)
            repo.delete_server(session, failed_id)
