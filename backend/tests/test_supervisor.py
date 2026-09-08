"""Supervisor reconcile tests — the parts that don't spawn real bridge processes.

Focus: a slug rename must converge onto a running unit's in-memory routing key.
slug is excluded from ``config_hash`` (a rename must not bounce the bridge), so the
reconciler — not a restart — is what keeps a live unit's ``slug`` in sync with
desired state. This guards the race where ``rename_slug`` missed a unit that didn't
exist yet and was then started from a pre-rename snapshot.
"""

from __future__ import annotations

import pytest

from datetime import timedelta
from types import SimpleNamespace

from sqlmodel import Session

from mcp.types import Tool

from app.db import get_engine, init_db, repo
from app.registry import service
from app.registry import settings as runtime_settings
from app.supervisor.supervisor import Supervisor
from app.supervisor.unit import ServerUnit, tool_summary

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
        "input_schema": {"type": "object"},
        "has_output_schema": True,
    }
    assert tool_summary(without_schema) == {
        "name": "bare",
        "description": "",
        "input_schema": {"type": "object"},
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


async def test_reconcile_forgets_activation_requests_for_deleted_servers():
    """Reconcile only consumes an activation request while iterating servers that EXIST,
    so one queued against a server being deleted — an operator restart racing the
    delete's own teardown — would sit in the map forever. The sweep drops any id the
    server table no longer knows, so the cleanup can't depend on every caller's cancel
    ordering."""
    with Session(get_engine()) as session:
        live = service.create_server(
            session, name="Still here", runner="command", command="/bin/true"
        )
        live_id = live.id
    sup = Supervisor()
    try:
        sup.request_activation("deleted-id-that-has-no-row")
        sup.request_activation(live_id)  # a real, disabled server keeps its request path
        await sup.reconcile_once()

        assert sup.activation_requested_at("deleted-id-that-has-no-row") is None
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, live_id)


async def test_reconcile_starts_requested_activations_before_starved_rows(monkeypatch):
    """A restart frees the very slot it means to reuse. The start loop otherwise walks
    enabled rows in created_at order and starts every unitless one, so at ``max_running``
    an older row that had been starved would take that slot — and the server the operator
    just restarted would come back "max_running reached" after an endpoint that reported
    it starting. Whoever was asked for goes first."""
    with Session(get_engine()) as session:
        older = service.create_server(
            session, name="Older starved", runner="command", command="/bin/true"
        )
        newer = service.create_server(
            session, name="Newer restarted", runner="command", command="/bin/true"
        )
        service.set_enabled(session, older.id, True)
        service.set_enabled(session, newer.id, True)
        older_id, newer_id = older.id, newer.id

    sup = Supervisor()
    started: list[str] = []

    async def record_start(server, *, activation_started_at=None):
        started.append(server.id)
        return None

    monkeypatch.setattr(sup, "_try_start", record_start)
    try:
        sup.request_activation(newer_id)  # what restart queues
        await sup.reconcile_once()

        assert started[0] == newer_id, started
        assert older_id in started  # the starved row still gets its turn, just after
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, older_id)
            repo.delete_server(session, newer_id)


async def test_stop_keeps_the_unit_when_teardown_raises():
    """`_stop` pops the unit before awaiting its teardown. If that teardown raises, the
    process (or container) may still be alive — and a dropped unit reads to the next
    reconcile as "no unit for a desired server", which would launch a SECOND copy beside
    it. The unit goes back in the map so the id stays accounted for and the next pass
    retries the stop."""
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Wedged", runner="command", command="/bin/true"
        )
        server_id = server.id
        unit = ServerUnit(server)
    sup = Supervisor()
    try:
        async def wedged_stop():
            raise RuntimeError("docker daemon is wedged")

        unit.stop = wedged_stop  # type: ignore[method-assign]
        sup.units[server_id] = unit

        with pytest.raises(RuntimeError):
            await sup._stop(server_id)

        assert sup.units.get(server_id) is unit  # still accounted for, not "absent"
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, server_id)


async def test_reconcile_retries_a_quarantined_teardown_and_starts_after_it(monkeypatch):
    """A unit whose ``stop()`` raised is kept (so no second copy is launched beside a
    process that may still be alive) — but a KEPT unit is invisible to the sweep: for a
    still-desired server with an unchanged ``config_hash`` and a "stopping" unit, no
    re-derive branch matches, so it would sit there enabled-but-unreachable until an
    operator acted by hand. ``_teardown_failed`` is what makes the next pass retry the
    stop, and start the server once that succeeds."""
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Wedged then freed", runner="command", command="/bin/true"
        )
        service.set_enabled(session, server.id, True)
        session.refresh(server)
        server_id = server.id
        unit = SimpleNamespace(**vars(_fake_unit(server)))

    sup = Supervisor()
    attempts: list[str] = []
    started: list[str] = []

    async def wedged_then_freed():
        attempts.append(server_id)
        unit.state = "stopping"
        if len(attempts) == 1:
            raise RuntimeError("docker daemon is wedged")

    unit.stop = wedged_then_freed
    sup.units[server_id] = unit

    async def record_start(server, *, activation_started_at=None):
        started.append(server.id)
        sup.units[server.id] = _fake_unit(server)
        return None

    monkeypatch.setattr(sup, "_try_start", record_start)
    try:
        # The operator's restart: the stop raises, so no activation is queued.
        with pytest.raises(RuntimeError):
            await sup.restart(server_id)
        assert sup.units[server_id] is unit
        assert started == []

        # The sweep retries the teardown it was left with, and only then starts. (The
        # retry carries a backoff — see the next test — so let this one's lapse.)
        quarantined = sup._teardown_failed[server_id]
        sup._teardown_failed[server_id] = quarantined._replace(
            retry_at=quarantined.retry_at - timedelta(seconds=quarantined.delay + 1)
        )
        await sup.reconcile_once()

        assert attempts == [server_id, server_id]
        assert started == [server_id]
        assert server_id not in sup._teardown_failed
    finally:
        sup.units.clear()
        with Session(get_engine()) as session:
            repo.delete_server(session, server_id)


async def test_reconcile_keeps_sweeping_when_one_teardown_fails(monkeypatch):
    """One wedged unit must not abandon the pass. A stop that raises used to propagate
    out of the sweep, so every server after it in the loop went unstarted (and unwritten)
    for as long as that one stayed wedged — which is exactly when the others most need
    converging."""
    with Session(get_engine()) as session:
        wedged = service.create_server(
            session, name="Wedged", runner="command", command="/bin/true"
        )
        other = service.create_server(
            session, name="Healthy neighbour", runner="command", command="/bin/true"
        )
        service.set_enabled(session, wedged.id, True)
        service.set_enabled(session, other.id, True)
        session.refresh(wedged)
        wedged_id, other_id = wedged.id, other.id
        unit = SimpleNamespace(**vars(_fake_unit(wedged)))

    sup = Supervisor()
    started: list[str] = []

    async def always_wedged():
        unit.state = "stopping"
        raise RuntimeError("docker daemon is wedged")

    unit.stop = always_wedged
    sup.units[wedged_id] = unit
    sup.request_activation(wedged_id)  # a restart of the wedged one

    async def record_start(server, *, activation_started_at=None):
        started.append(server.id)
        sup.units[server.id] = _fake_unit(server)
        return None

    monkeypatch.setattr(sup, "_try_start", record_start)
    try:
        await sup.reconcile_once()

        # The wedged unit is kept and NOT restarted (its process may still be alive),
        # its failure is on the row, and the neighbour was started all the same.
        assert sup.units[wedged_id] is unit
        assert wedged_id in sup._teardown_failed
        assert started == [other_id]
        assert "stop failed" in (unit.last_error or "")
    finally:
        sup.units.clear()
        with Session(get_engine()) as session:
            repo.delete_server(session, wedged_id)
            repo.delete_server(session, other_id)


async def test_reconcile_backs_off_repeated_teardown_attempts(monkeypatch):
    """A stop that fails FAST would otherwise spin the loop: ``unit.stop()`` sets
    "stopping" before it raises, that state notification nudges the supervisor, and
    ``run_forever`` then skips its interval wait — straight into an identical attempt.
    A quarantined unit is retried on a backoff instead (an operator's own stop still
    isn't throttled: that path goes through ``_stop``)."""
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Always wedged", runner="command", command="/bin/true"
        )
        service.set_enabled(session, server.id, True)
        session.refresh(server)
        server_id = server.id
        unit = SimpleNamespace(**vars(_fake_unit(server)))

    sup = Supervisor()
    attempts: list[str] = []

    async def always_wedged():
        attempts.append(server_id)
        unit.state = "stopping"
        raise RuntimeError("docker daemon is wedged")

    unit.stop = always_wedged
    sup.units[server_id] = unit
    sup.request_activation(server_id)
    try:
        await sup.reconcile_once()
        assert len(attempts) == 1

        # The nudge the failed attempt raised brings the next sweep immediately; the
        # backoff is what keeps it from repeating the teardown.
        await sup.reconcile_once()
        assert len(attempts) == 1

        # Once it comes due, the retry happens — and the next delay is longer.
        first = sup._teardown_failed[server_id]
        sup._teardown_failed[server_id] = first._replace(
            retry_at=first.retry_at - timedelta(seconds=first.delay + 1)
        )
        await sup.reconcile_once()
        assert len(attempts) == 2
        assert sup._teardown_failed[server_id].delay > first.delay

        # An operator asking for it directly is never throttled.
        with pytest.raises(RuntimeError):
            await sup.restart(server_id)
        assert len(attempts) == 3
    finally:
        sup.units.clear()
        with Session(get_engine()) as session:
            repo.delete_server(session, server_id)
