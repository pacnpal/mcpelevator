"""Per-server health checks (issue #225).

A health check proves more than "the bridge answers tools/list": a tool call through the
bridge, or (local runners) a shell command run like the setup script. It runs at
readiness, inside the startup deadline, where a failure fails the attempt; and, with an
interval, periodically against a running unit, where a failure marks it unhealthy (no
restart) until a later check passes. It lives outside config_hash, never counts as usage
or as traffic, and never runs against an idle server.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from conftest import LOOPBACK

from app.api import health as health_api
from app.db import get_engine, repo
from app.db.models import Server
from app.main import app
from app.registry import service
from app.supervisor import unit as unit_module
from app.supervisor.supervisor import Supervisor
from app.supervisor.unit import HealthCheck, ServerUnit

FIXTURE = Path(__file__).with_name("health_stdio_fixture.py")
PLAYWRIGHT_ERROR = (
    "browserType.launchPersistentContext: Executable doesn't exist at "
    "/root/.cache/ms-playwright/chromium-1179/chrome-linux/chrome"
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for(predicate, *, timeout: float = 15) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def _settings(tmp_path: Path, **overrides):
    values = {
        "bridge_host": "127.0.0.1",
        "backend_root": Path(__file__).parents[1],
        "data_dir": tmp_path,
        "start_timeout_s": 15.0,
        "restart_budget": 2,
        "restart_stable_s": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def fast_unit(tmp_path, monkeypatch):
    """Unit settings for these tests, with the retry backoff cut to a blink."""
    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(unit_module, "_BACKOFF_SECONDS", (0.05,))
    work = tmp_path / "work"
    work.mkdir()
    return work


def _server(work: Path, **health) -> Server:
    return Server(
        id="health-test",
        slug="health-test",
        name="Health test",
        runner="command",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"HEALTH_ENV": "from-env"},
        cwd=str(work),
        config_hash="hash",
        **health,
    )


# --- readiness --------------------------------------------------------------- #


async def test_tool_check_that_errors_fails_the_activation_after_retries(fast_unit):
    (fast_unit / "health-fail").write_text("database unreachable")
    unit = ServerUnit(_server(fast_unit, health_check_tool="check"))
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state == "failed")
        assert "health check failed" in (unit.last_error or "")
        assert "database unreachable" in (unit.last_error or "")
        logs = unit.logs.snapshot()
        # Both attempts reached the health phase, after a successful tools/list.
        assert any("attempt 1/2: health" in line for line in logs)
        assert any("attempt 2/2: health" in line for line in logs)
        assert any(line.startswith("[health] ") and "database unreachable" in line for line in logs)
        assert unit.port is None
    finally:
        await unit.stop()


async def test_tool_check_that_passes_reaches_running(fast_unit):
    unit = ServerUnit(
        _server(fast_unit, health_check_tool="check", health_check_arguments={"echo": "hi"})
    )
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in ("running", "failed"))
        assert unit.state == "running", "\n".join(unit.logs.snapshot())
        logs = unit.logs.snapshot()
        assert "[health] hi" in logs
        assert any("health check passed (tool check)" in line for line in logs)
        assert unit.last_error is None
    finally:
        await unit.stop()


async def test_failing_shell_check_fails_the_attempt(fast_unit):
    unit = ServerUnit(
        _server(fast_unit, health_check_command="echo 'port 3000 refused'\nexit 1")
    )
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state == "failed")
        assert "health check failed: exited with code 1" in (unit.last_error or "")
        assert "port 3000 refused" in (unit.last_error or "")
        assert "[health] port 3000 refused" in unit.logs.snapshot()
    finally:
        await unit.stop()


async def test_failure_quoting_the_output_hides_secret_env_values(fast_unit):
    # last_error reaches the API and the container log; a check that echoes a hidden
    # value must not put it there.
    from app import sealed

    server = _server(fast_unit, health_check_command='echo "key=$API_KEY"\nexit 3')
    server.env = {**server.env, "API_KEY": sealed.seal("plain-secret-value-123")}
    unit = ServerUnit(server)
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state == "failed")
        assert "key=<hidden>" in (unit.last_error or "")
        assert "plain-secret-value-123" not in (unit.last_error or "")
    finally:
        await unit.stop()


async def test_a_secret_across_the_failure_cut_is_hidden_whole(fast_unit):
    """The failure text is cut at _HEALTH_FAILURE_CHARS; a secret straddling that cut must
    be hidden before it, never leaving a prefix in last_error (tool form)."""
    from app import sealed

    secret = "sk-live-0123456789abcdef"
    (fast_unit / "health-fail").write_text("x" * 270 + " " + secret)
    server = _server(fast_unit, health_check_tool="check")
    server.env = {**server.env, "API_KEY": sealed.seal(secret)}
    unit = ServerUnit(server)
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state == "failed")
        assert secret[:6] not in (unit.last_error or "")
        assert "<hidden>" in (unit.last_error or "")
    finally:
        await unit.stop()


async def test_a_secret_across_an_output_line_cap_is_hidden_whole(fast_unit):
    """A command's output line is capped at _HEALTH_LINE_BYTES; a secret straddling the
    cap is hidden before it, so no prefix reaches the failure text or the output."""
    from app import sealed

    secret = "sk-live-0123456789abcdef"
    pad = unit_module._HEALTH_LINE_BYTES - 6
    server = _server(fast_unit)
    server.env = {**server.env, "API_KEY": sealed.seal(secret)}
    unit = ServerUnit(server)
    check = HealthCheck(command=f'printf "%0{pad}d%s\\n" 0 "$API_KEY"\nexit 2')
    failure, output = await unit._health_check_once(check, 10)
    assert failure is not None and failure.startswith("exited with code 2")
    # Hidden whole, then cut: the cap may shorten the marker, never expose the secret.
    assert secret[:6] not in failure and "<hid" in failure
    assert not any(secret[:6] in line for line in output)


async def test_a_secret_after_multibyte_output_is_hidden_whole(fast_unit):
    """The output cap and the secret lookahead are both in bytes: multibyte text before a
    secret must not let a character cut keep a prefix the byte lookahead never covered."""
    from app import sealed

    secret = "sk-live-0123456789abcdef"
    server = _server(fast_unit)
    server.env = {**server.env, "API_KEY": sealed.seal(secret)}
    unit = ServerUnit(server)
    euros = "€" * 1367  # 4,101 bytes, 1,367 characters
    check = HealthCheck(command=f'printf "%s%s\\n" "{euros}" "$API_KEY"\nexit 2')
    failure, output = await unit._health_check_once(check, 10)
    assert failure is not None
    assert secret[:6] not in failure
    assert not any(secret[:6] in line for line in output)
    assert all(len(line.encode()) <= unit_module._HEALTH_LINE_BYTES for line in output)


async def test_a_multiline_secret_in_command_output_is_hidden_whole(fast_unit):
    """A secret spanning lines (a PEM key) is hidden over the whole output before it is
    split into lines, so neither the output nor the failure text keeps a line of it."""
    from app import sealed

    secret = "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END KEY-----"
    server = _server(fast_unit)
    server.env = {**server.env, "API_KEY": sealed.seal(secret)}
    unit = ServerUnit(server)
    check = HealthCheck(command='printf "loading\\n%s\\n" "$API_KEY"\nexit 2')
    failure, output = await unit._health_check_once(check, 10)
    assert output == ["loading", "<hidden>"]
    assert "MIIEvQ" not in failure and "BEGIN KEY" not in failure


async def test_a_passing_command_with_a_huge_output_line_still_passes(fast_unit):
    # One line past asyncio's 64 KiB StreamReader limit (a big JSON response): kept
    # truncated, never a failure of a command that exited 0.
    unit = ServerUnit(
        _server(fast_unit, health_check_command="head -c 70000 /dev/zero | tr '\\0' x; echo")
    )
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in ("running", "failed"))
        assert unit.state == "running", unit.last_error
        kept = [line for line in unit.logs.snapshot() if line.startswith("[health] x")]
        assert kept and len(kept[0]) <= len("[health] ") + unit_module._HEALTH_LINE_BYTES
    finally:
        await unit.stop()


async def test_passing_shell_check_runs_with_the_child_env_and_cwd(fast_unit):
    unit = ServerUnit(
        _server(
            fast_unit,
            health_check_command=(
                'printf "%s|%s\\n" "$HEALTH_ENV" "$PWD" > health-ran\n'
                'test "$HEALTH_ENV" = from-env'
            ),
        )
    )
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in ("running", "failed"))
        assert unit.state == "running", "\n".join(unit.logs.snapshot())
        assert (fast_unit / "health-ran").read_text().strip() == f"from-env|{fast_unit}"
    finally:
        await unit.stop()


async def test_health_phase_failure_carries_the_playwright_hint(fast_unit, monkeypatch):
    monkeypatch.setattr(
        unit_module, "get_settings", lambda: _settings(fast_unit.parent, restart_budget=1)
    )
    (fast_unit / "health-fail").write_text(PLAYWRIGHT_ERROR)
    unit = ServerUnit(_server(fast_unit, health_check_tool="check"))
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state == "failed")
        assert "health check failed" in (unit.last_error or "")
        assert "npx playwright install chromium" in (unit.last_error or "")
    finally:
        await unit.stop()


# --- periodic ---------------------------------------------------------------- #


def _make_server(**kwargs) -> Server:
    with Session(get_engine()) as session:
        return service.create_server(session, **kwargs)


def _delete(server_id: str) -> None:
    with Session(get_engine()) as session:
        repo.delete_server(session, server_id)


def _health_app(sup: Supervisor) -> FastAPI:
    """Just the health routes over ``sup`` — no lifespan, so no second supervisor."""
    mini = FastAPI()
    mini.include_router(health_api.router, prefix="/api")
    mini.state.supervisor = sup
    return mini


async def _run_due_check(sup: Supervisor, unit: ServerUnit) -> None:
    unit._health_last_at -= 3600  # the interval has passed
    await sup.reconcile_once()
    assert unit._health_task is not None
    await asyncio.wait_for(asyncio.shield(unit._health_task), 15)


async def test_periodic_failure_marks_unhealthy_without_restart_then_recovers(
    tmp_path, fast_unit, monkeypatch
):
    monkeypatch.setattr(
        unit_module,
        "get_settings",
        lambda: _settings(tmp_path, restart_budget=1, restart_stable_s=0.05),
    )
    server = _make_server(
        name="Periodic health",
        runner="command",
        command=sys.executable,
        args=[str(FIXTURE)],
        cwd=str(fast_unit),
        health_check_tool="check",
        health_check_interval_s=30,
        enabled=True,
    )
    sid = server.id
    sup = Supervisor()
    try:
        await sup.reconcile_once()
        unit = sup.unit(sid)
        assert isinstance(unit, ServerUnit)
        await _wait_for(lambda: unit.state in ("running", "failed"))
        assert unit.state == "running", "\n".join(unit.logs.snapshot())
        pid = unit.pid
        api = TestClient(_health_app(sup))
        assert api.get(f"/api/health/{server.slug}", headers=LOOPBACK).status_code == 200

        # Not due yet: nothing runs.
        await sup.reconcile_once()
        assert unit._health_task is None

        (fast_unit / "health-fail").write_text("upstream token expired")
        activity = sup._last_activity.get(sid)
        await _run_due_check(sup, unit)
        assert unit.state == "unhealthy"
        assert unit.health_failing
        assert "health check failed: upstream token expired" in (unit.last_error or "")
        # A check is not traffic: it can't hold an idle window open.
        assert sup._last_activity.get(sid) == activity
        r = api.get(f"/api/health/{server.slug}", headers=LOOPBACK)
        assert r.status_code == 503
        assert r.json()["status"] == "unavailable"

        # The supervisor leaves it be: same unit, same bridge process, state persisted.
        await sup.reconcile_once()
        assert sup.unit(sid) is unit and unit.pid == pid
        with Session(get_engine()) as session:
            runtime = repo.get_runtime(session, sid)
        assert runtime.state == "unhealthy"
        assert "upstream token expired" in (runtime.last_error or "")

        # A later passing check returns it to running and clears the error.
        (fast_unit / "health-fail").unlink()
        await _run_due_check(sup, unit)
        assert unit.state == "running"
        assert not unit.health_failing
        assert unit.last_error is None
        assert unit.pid == pid
        assert api.get(f"/api/health/{server.slug}", headers=LOOPBACK).status_code == 200
        assert any("back to running" in line for line in unit.logs.snapshot())
    finally:
        await sup.shutdown()
        _delete(sid)


async def test_a_replaced_checks_result_never_marks_the_server(fast_unit, monkeypatch):
    """A periodic check edited while it ran must not set the state from the old check's
    verdict; the new check is due at once instead."""
    unit = ServerUnit(_server(fast_unit, health_check_tool="check", health_check_interval_s=10))
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in ("running", "failed"))
        assert unit.state == "running"
        old = unit.health_check
        release = asyncio.Event()

        async def slow_failure(check, timeout):
            await release.wait()
            return "old check failed", []

        monkeypatch.setattr(unit, "_health_check_once", slow_failure)
        task = asyncio.create_task(unit._periodic_health_check(old))
        await asyncio.sleep(0)
        unit.health_check = HealthCheck(tool="ping", interval_s=10)  # edited meanwhile
        release.set()
        await task
        assert unit.state == "running" and not unit.health_failing
        assert unit._health_last_at == float("-inf")  # the new check is due now
    finally:
        await unit.stop()


async def test_removing_the_check_while_failing_returns_to_running(fast_unit):
    unit = ServerUnit(_server(fast_unit, health_check_tool="check", health_check_interval_s=30))
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in ("running", "failed"))
        assert unit.state == "running"
        (fast_unit / "health-fail").write_text("nope")
        unit._health_last_at -= 3600
        assert unit.tick_health_check(unit.health_check)
        await unit._health_task
        assert unit.state == "unhealthy" and unit.health_failing
        # The operator drops the check: nothing says it's unhealthy any more.
        assert not unit.tick_health_check(None)
        assert unit.state == "running" and unit.last_error is None
    finally:
        await unit.stop()


def test_tick_never_checks_a_unit_that_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    unit = ServerUnit(_server(tmp_path, health_check_tool="check", health_check_interval_s=10))
    check = unit.health_check
    assert check is not None and check.interval_s == 10

    async def go():
        for state in ("stopped", "starting", "idle", "unhealthy", "failed"):
            unit.state = state
            unit._health_last_at = -1e9
            assert not unit.tick_health_check(check), state

    asyncio.run(go())


class _IdleableUnit(SimpleNamespace):
    async def stop(self):
        self.state = "stopped"

    def tick_health_check(self, check):
        self.ticks.append(check)
        return True


async def test_idle_server_is_never_health_checked():
    from datetime import timedelta

    from app.db.models import utcnow

    server = _make_server(
        name="Idle health",
        runner="npx",
        command="npx",
        enabled=True,
        idle_timeout_s=30,
        health_check_tool="check",
        health_check_interval_s=10,
    )
    sid = server.id
    sup = Supervisor()
    unit = _IdleableUnit(
        slug=server.slug, config_hash=server.config_hash, state="running", pid=1, port=49999,
        last_error=None, tools=[], restart_count=0, last_health=None, startup_status=None,
        ticks=[],
    )
    sup.units[sid] = unit
    sup._last_activity[sid] = utcnow() - timedelta(seconds=31)

    async def no_start(sv, *, activation_started_at=None):
        raise AssertionError("an idle server must not be started")

    sup._try_start = no_start
    try:
        await sup.reconcile_once()  # quiesced this pass, never checked
        assert sup.is_idle(sid) and sid not in sup.units
        await sup.reconcile_once()  # and still nothing to check while idle
        assert unit.ticks == []
    finally:
        sup.units.pop(sid, None)
        _delete(sid)


async def test_a_health_failing_unit_still_idles():
    """A live bridge failing its periodic check is unhealthy but up; unused past its idle
    window it must sleep like a running one, not stay up running checks forever."""
    from datetime import timedelta

    from app.db.models import utcnow

    server = _make_server(
        name="Idle failing",
        runner="npx",
        command="npx",
        enabled=True,
        idle_timeout_s=30,
        health_check_tool="check",
        health_check_interval_s=10,
    )
    sid = server.id
    sup = Supervisor()
    unit = _IdleableUnit(
        slug=server.slug, config_hash=server.config_hash, state="unhealthy", pid=1,
        port=49999, last_error="health check failed: boom", tools=[], restart_count=0,
        last_health=None, startup_status=None, ticks=[], health_failing=True,
    )
    sup.units[sid] = unit
    sup._last_activity[sid] = utcnow() - timedelta(days=1)

    async def no_start(sv, *, activation_started_at=None):
        raise AssertionError("an idled server must not be started")

    sup._try_start = no_start
    try:
        await sup.reconcile_once()
        assert sup.is_idle(sid) and sid not in sup.units
        assert unit.ticks == []
    finally:
        sup.units.pop(sid, None)
        _delete(sid)


async def test_reconcile_hands_the_running_unit_its_current_check():
    server = _make_server(
        name="Ticked", runner="npx", command="npx", enabled=True,
        health_check_tool="check", health_check_arguments={"x": 1}, health_check_interval_s=20,
    )
    sid = server.id
    sup = Supervisor()
    unit = _IdleableUnit(
        slug=server.slug, config_hash=server.config_hash, state="running", pid=1, port=49998,
        last_error=None, tools=[], restart_count=0, last_health=None, startup_status=None,
        ticks=[],
    )
    sup.units[sid] = unit
    sup.mark_activity(sid)
    try:
        await sup.reconcile_once()
        assert unit.ticks == [HealthCheck(tool="check", arguments={"x": 1}, interval_s=20)]
        # An edit lands on the live unit without touching config_hash (no restart).
        with Session(get_engine()) as session:
            before = repo.get_server(session, sid).config_hash
            service.update_server(session, sid, {"health_check_interval_s": None})
            assert repo.get_server(session, sid).config_hash == before
        await sup.reconcile_once()
        assert sup.units[sid] is unit
        assert unit.ticks[-1] == HealthCheck(tool="check", arguments={"x": 1})
    finally:
        sup.units.pop(sid, None)
        _delete(sid)


# --- save-time validation ------------------------------------------------------ #


def _post(client, **fields):
    return client.post(
        "/api/servers", json={"name": "hc", "command": "echo", **fields}, headers=LOOPBACK
    )


def test_health_check_validation_on_create():
    with TestClient(app) as client:
        bad = [
            # one form, not both
            {"health_check_tool": "ping", "health_check_command": "true"},
            # a command runs on this host: local runners only
            {"runner": "remote", "command": "https://example.com/mcp", "health_check_command": "true"},
            {"runner": "openapi", "command": "https://example.com/openapi.json",
             "health_check_command": "true"},
            {"runner": "docker", "command": "ghcr.io/acme/server:1", "health_check_command": "true"},
            # interval: a floor, a ceiling, and only with a check
            {"health_check_tool": "ping", "health_check_interval_s": 5},
            {"health_check_tool": "ping", "health_check_interval_s": 86401},
            {"health_check_interval_s": 60},
            # arguments belong to a tool check
            {"health_check_arguments": {"a": 1}},
        ]
        for fields in bad:
            r = _post(client, **fields)
            assert r.status_code == 400, (fields, r.text)
        assert _post(client, health_check_tool="ping", health_check_interval_s=True).status_code == 422
        assert _post(client, health_check_tool="ping", health_check_arguments=[1]).status_code == 422

        # A tool check works for any runner, the remote one included.
        r = _post(
            client, runner="remote", command="https://example.com/mcp",
            health_check_tool="ping", health_check_arguments={"q": "x"}, health_check_interval_s=60,
        )
        assert r.status_code == 201, r.text
        client.delete(f"/api/servers/{r.json()['id']}", headers=LOOPBACK)


def test_health_check_round_trips_patch_and_clone_without_a_restart():
    with TestClient(app) as client:
        r = _post(client, health_check_command="  curl -fsS http://127.0.0.1:3000/health\n",
                  health_check_interval_s=60)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        clone_id = None
        try:
            detail = client.get(f"/api/servers/{sid}", headers=LOOPBACK).json()
            assert detail["health_check_command"].strip() == "curl -fsS http://127.0.0.1:3000/health"
            assert detail["health_check_interval_s"] == 60
            assert detail["health_check_tool"] is None
            with Session(get_engine()) as session:
                before = repo.get_server(session, sid).config_hash

            # Switch forms: the unused one is sent as null, blank reads as unset.
            r = client.patch(
                f"/api/servers/{sid}",
                json={"health_check_command": None, "health_check_tool": "ping",
                      "health_check_arguments": {"n": 2}},
                headers=LOOPBACK,
            )
            assert r.status_code == 200, r.text
            detail = client.get(f"/api/servers/{sid}", headers=LOOPBACK).json()
            assert detail["health_check_tool"] == "ping"
            assert detail["health_check_arguments"] == {"n": 2}
            assert detail["health_check_command"] is None
            assert detail["health_check_interval_s"] == 60
            # Both at once is still refused on an edit.
            r = client.patch(
                f"/api/servers/{sid}", json={"health_check_command": "true"}, headers=LOOPBACK
            )
            assert r.status_code == 400
            with Session(get_engine()) as session:
                assert repo.get_server(session, sid).config_hash == before

            r = client.post(f"/api/servers/{sid}/clone", json={}, headers=LOOPBACK)
            assert r.status_code in (200, 201), r.text
            clone_id = r.json()["id"]
            copy = client.get(f"/api/servers/{clone_id}", headers=LOOPBACK).json()
            assert copy["health_check_tool"] == "ping"
            assert copy["health_check_arguments"] == {"n": 2}
            assert copy["health_check_interval_s"] == 60

            # Clearing: explicit nulls, all of them = no check.
            r = client.patch(
                f"/api/servers/{sid}",
                json={"health_check_tool": None, "health_check_arguments": None,
                      "health_check_interval_s": None},
                headers=LOOPBACK,
            )
            assert r.status_code == 200, r.text
            detail = client.get(f"/api/servers/{sid}", headers=LOOPBACK).json()
            assert detail["health_check_tool"] is None
            assert detail["health_check_arguments"] is None
        finally:
            client.delete(f"/api/servers/{sid}", headers=LOOPBACK)
            if clone_id:
                client.delete(f"/api/servers/{clone_id}", headers=LOOPBACK)


def test_converting_to_a_remote_runner_refuses_a_leftover_command():
    with TestClient(app) as client:
        r = _post(client, health_check_command="true")
        sid = r.json()["id"]
        try:
            r = client.patch(
                f"/api/servers/{sid}",
                json={"runner": "remote", "command": "https://example.com/mcp"},
                headers=LOOPBACK,
            )
            assert r.status_code == 400
            assert "Health check commands" in r.json()["detail"]
        finally:
            client.delete(f"/api/servers/{sid}", headers=LOOPBACK)


def test_a_health_command_is_a_retarget_for_hidden_env():
    """The command runs with the server's decrypted env and its output reaches the log,
    so adding or changing one must re-enter the hidden values, like a setup script."""
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Sealed", runner="command", command="server",
            env={"API_KEY": "s3cret-value"}, secret_env=["API_KEY"],
        )
    sid = server.id
    try:
        with Session(get_engine()) as session:
            with pytest.raises(ValueError):
                service.update_server(
                    session, sid,
                    {"health_check_command": "printenv API_KEY", "env": {"API_KEY": "<hidden>"}},
                )
        with Session(get_engine()) as session:
            assert repo.get_server(session, sid).health_check_command is None
            # A tool check runs nothing on the host: no secret needs re-entering.
            service.update_server(
                session, sid, {"health_check_tool": "ping", "env": {"API_KEY": "<hidden>"}}
            )
    finally:
        _delete(sid)


def test_a_health_command_calling_docker_is_refused_on_an_enabled_server():
    with Session(get_engine()) as session:
        with pytest.raises(ValueError, match="docker runner"):
            service.create_server(
                session, name="Sneaky", runner="command", command="server",
                health_check_command="docker run --rm alpine true", enabled=True,
            )
