from __future__ import annotations

import asyncio
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.db.models import Server
from app.supervisor.unit import ServerUnit


FIXTURE = Path(__file__).with_name("stdio_server_fixture.py")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for(predicate, *, timeout: float = 8) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def _settings(tmp_path: Path, **overrides):
    values = {
        "bridge_host": "127.0.0.1",
        "backend_root": Path(__file__).parents[1],
        "data_dir": tmp_path,
        "start_timeout_s": 3.0,
        "restart_budget": 2,
        "restart_stable_s": 0.1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _server(tmp_path: Path, *, setup_script: str) -> Server:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Server(
        id="unit-test",
        slug="unit-test",
        name="Unit test",
        runner="command",
        command=sys.executable,
        args=[str(FIXTURE)],
        env={"EXPECTED_CWD": str(work)},
        cwd=str(work),
        setup_script=setup_script,
        config_hash="hash",
    )


def test_env_scrub_keeps_control_plane_secrets_from_children(tmp_path, monkeypatch):
    # The break-glass admin token must not reach an untrusted local-exec child — not in its own
    # env, and not recoverable from the (trusted) bridge parent's env via /proc/<ppid>/environ.
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setenv("MCPE_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("MCPE_PUBLIC_BASE_URL", "http://x")  # non-secret MCPE_ config
    monkeypatch.setenv("PATH", "/usr/bin")
    unit = ServerUnit(_server(tmp_path, setup_script="true"))
    unit.port = 12345

    bridge_env = unit._bridge_env(unit._bridge_payload())
    assert "MCPE_ADMIN_TOKEN" not in bridge_env               # secret kept out of the bridge parent
    assert bridge_env["MCPE_PUBLIC_BASE_URL"] == "http://x"   # non-secret config still available
    assert bridge_env["PATH"] == "/usr/bin"

    # The setup script gets the full child scrub: ALL of the elevator's MCPE_ namespace removed,
    # while the server's own vars and ordinary tooling env survive.
    setup_env = unit._effective_child_env()
    assert "MCPE_ADMIN_TOKEN" not in setup_env
    assert "MCPE_PUBLIC_BASE_URL" not in setup_env            # a child needs no elevator config
    assert setup_env["EXPECTED_CWD"] == str(tmp_path / "work")  # server var kept
    assert setup_env["PATH"] == "/usr/bin"


async def test_setup_runs_before_bridge_with_child_environment_and_cwd(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    server = _server(
        tmp_path,
        setup_script=(
            "printf '%s|%s\\n' \"$EXPECTED_CWD\" \"$PWD\" > setup-env\n"
            "printf 'setup stdout\\n'\n"
            "printf 'setup stderr\\n' >&2\n"
            "touch setup-complete\n"
            "export SETUP_ONLY=must-not-leak\n"
            "cd /\n"
        ),
    )
    unit = ServerUnit(server)

    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in {"running", "failed"})
        assert unit.state == "running", "\n".join(unit.logs.snapshot())
        work = Path(server.cwd or "")
        assert (work / "setup-env").read_text().strip() == f"{work}|{work}"
        logs = unit.logs.snapshot()
        assert any("attempt 1/2: setup" in line for line in logs)
        assert "setup stdout" in logs
        assert "setup stderr" in logs
        assert any("attempt 1/2: bridge" in line for line in logs)
    finally:
        await unit.stop()


async def test_setup_failure_retries_then_becomes_terminal(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    server = _server(
        tmp_path,
        setup_script=(
            "n=0; [ ! -f attempts ] || n=$(cat attempts)\n"
            "n=$((n + 1)); printf '%s\\n' \"$n\" > attempts\n"
            "printf 'setup failed\\n'\n"
            "exit 7\n"
        ),
    )
    released: list[int] = []
    unit = ServerUnit(server, release_port=released.append)

    port = _port()
    await unit.start(port)
    await _wait_for(
        lambda: unit.startup_status is not None
        and unit.startup_status.phase == "retry_wait"
    )
    assert unit.startup_status is not None
    assert unit.startup_status.next_retry_at is not None
    retry_delay = (
        unit.startup_status.next_retry_at - datetime.now(timezone.utc)
    ).total_seconds()
    assert 1.5 <= retry_delay <= 2.1
    await _wait_for(lambda: unit.state == "failed")

    assert (Path(server.cwd or "") / "attempts").read_text().strip() == "2"
    assert "setup exited with code 7" in (unit.last_error or "")
    logs = unit.logs.snapshot()
    assert sum("setup failed" in line for line in logs) == 2
    assert not any("starting bridge" in line for line in logs)
    assert released == [port]
    assert unit.port is None
    await unit.stop()
    assert released == [port]


async def test_setup_timeout_prevents_bridge_launch(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(
        unit_module,
        "get_settings",
        lambda: _settings(tmp_path, start_timeout_s=0.1, restart_budget=1),
    )
    unit = ServerUnit(_server(tmp_path, setup_script="sleep 60"))

    await unit.start(_port())
    await _wait_for(lambda: unit.state == "failed", timeout=3)

    assert "setup timed out after 0.1s" in (unit.last_error or "")
    assert not any("starting bridge" in line for line in unit.logs.snapshot())
    await unit.stop()


async def test_setup_timeout_kills_background_child_holding_logs_open(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(
        unit_module,
        "get_settings",
        lambda: _settings(tmp_path, start_timeout_s=0.1, restart_budget=1),
    )
    server = _server(
        tmp_path,
        setup_script=(
            "sh -c 'trap \"\" TERM; while :; do sleep 1; done' & "
            "echo $! > child-pid"
        ),
    )
    unit = ServerUnit(server)
    pid_file = Path(server.cwd or "") / "child-pid"

    await unit.start(_port())
    await _wait_for(lambda: unit.state == "failed", timeout=3)
    child_pid = int(pid_file.read_text())

    def child_is_gone() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        return False

    await _wait_for(child_is_gone, timeout=3)
    assert "setup timed out after 0.1s" in (unit.last_error or "")
    assert not any("starting bridge" in line for line in unit.logs.snapshot())
    await unit.stop()


async def test_stop_during_setup_kills_the_process_group(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(
        unit_module,
        "get_settings",
        lambda: _settings(tmp_path, start_timeout_s=60, restart_budget=1),
    )
    server = _server(
        tmp_path,
        setup_script="sleep 60 & echo $! > child-pid; wait",
    )
    unit = ServerUnit(server)
    pid_file = Path(server.cwd or "") / "child-pid"

    await unit.start(_port())
    await _wait_for(pid_file.exists)
    child_pid = int(pid_file.read_text())
    await unit.stop()

    def child_is_gone() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        return False

    await _wait_for(child_is_gone, timeout=3)
    assert unit.state == "stopped"
    assert unit.startup_status is None


async def test_stop_cancels_readiness_probe(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(
        unit_module,
        "get_settings",
        lambda: _settings(tmp_path, start_timeout_s=60, restart_budget=1),
    )
    unit = ServerUnit(_server(tmp_path, setup_script="touch setup-complete"))
    probe_started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def blocked_probe(url: str, timeout: float):
        probe_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            probe_cancelled.set()

    monkeypatch.setattr(unit, "_probe", blocked_probe)
    await unit.start(_port())
    await asyncio.wait_for(probe_started.wait(), timeout=3)
    await asyncio.wait_for(unit.stop(), timeout=3)

    assert probe_cancelled.is_set()
    assert unit.state == "stopped"


async def test_exit_after_stable_run_waits_before_fresh_activation(tmp_path, monkeypatch):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(
        unit_module,
        "get_settings",
        lambda: _settings(tmp_path, restart_budget=1, restart_stable_s=0.05),
    )
    server = _server(tmp_path, setup_script="touch setup-complete")
    unit = ServerUnit(server)

    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state == "running")
        await asyncio.sleep(0.1)
        assert unit.proc is not None
        os.killpg(unit.proc.pid, 15)

        await _wait_for(
            lambda: unit.startup_status is not None
            and unit.startup_status.phase == "retry_wait"
        )
        assert unit.state == "starting"
        assert unit.last_error is None
        await _wait_for(lambda: unit.state == "unhealthy", timeout=3)
        assert "bridge exited" in (unit.last_error or "")
    finally:
        await unit.stop()
