"""Live process state for one Server activation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastmcp import Client

from app.config import get_settings
from app.db.models import Server, utcnow
from app.runners import build_spec
from app.runners.docker import DOCKER_BIN, server_label
from app.supervisor.logbuffer import LogBuffer


_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0)
_READINESS_RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class StartupSnapshot:
    phase: str
    attempt: int
    max_attempts: int
    activation_started_at: datetime
    deadline_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    message: Optional[str] = None


class _AttemptFailed(RuntimeError):
    pass


def tool_summary(tool) -> dict:
    """One cached-tool entry for the UI, from a probed ``mcp.types.Tool``."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "has_output_schema": tool.outputSchema is not None,
    }


class ServerUnit:
    def __init__(
        self,
        server: Server,
        *,
        on_state_change: Optional[Callable[[], None]] = None,
        release_port: Optional[Callable[[int], None]] = None,
    ):
        # Snapshot primitives; never hold a live ORM object.
        self.id = server.id
        self.slug = server.slug
        self.name = server.name
        self.config_hash = server.config_hash
        self.runner = server.runner
        self.spec = build_spec(server)
        self.exposure = {"mcp_http": server.mcp_http, "rest_openapi": server.rest_openapi}

        self.host = get_settings().bridge_host
        self.port: Optional[int] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.setup_proc: Optional[asyncio.subprocess.Process] = None
        self.state = "stopped"
        self.last_error: Optional[str] = None
        self.tools: list[dict] = []
        self.restart_count = 0
        self.last_health: Optional[datetime] = None
        self.startup_status: Optional[StartupSnapshot] = None
        self.logs = LogBuffer()

        self._activation_started_at = utcnow()
        self._activation_task: Optional[asyncio.Task[None]] = None
        self._log_task: Optional[asyncio.Task[None]] = None
        self._setup_log_task: Optional[asyncio.Task[None]] = None
        self._stopping = False
        self._port_released = True
        self._on_state_change = on_state_change
        self._release_port_callback = release_port

    async def start(self, port: int, *, activation_started_at: Optional[datetime] = None) -> None:
        if self._activation_task is not None and not self._activation_task.done():
            return
        settings = get_settings()
        max_attempts = max(1, int(settings.restart_budget))
        self.port = port
        self._port_released = False
        self._stopping = False
        self.last_error = None
        self.tools = []
        self.restart_count = 0
        self.last_health = None
        self._activation_started_at = activation_started_at or utcnow()
        self._set_state("starting")
        self._set_status("queued", attempt=1, max_attempts=max_attempts)
        self.logs.append(f"[mcpelevator] activation started; max attempts={max_attempts}")
        self._activation_task = asyncio.create_task(self._run_activation(max_attempts))

    async def stop(self) -> None:
        self._stopping = True
        self.startup_status = None
        self._set_state("stopping")
        task = self._activation_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._cleanup_attempt()
        self._release_port()
        self.proc = None
        self.setup_proc = None
        self._set_state("stopped")

    async def _run_activation(self, max_attempts: int) -> None:
        last_failure = "startup failed"
        try:
            for attempt in range(1, max_attempts + 1):
                if self._stopping:
                    return
                self.restart_count = attempt - 1
                try:
                    stable, rc = await self._run_attempt(attempt, max_attempts)
                    if stable:
                        await self._cleanup_attempt()
                        failure = f"bridge exited rc={rc}"
                        self._release_port()
                        delay = _BACKOFF_SECONDS[0]
                        self.last_error = None
                        self._set_state("starting")
                        self._set_status(
                            "retry_wait",
                            attempt=1,
                            max_attempts=max_attempts,
                            next_retry_at=utcnow() + timedelta(seconds=delay),
                            message=failure,
                        )
                        self.logs.append(
                            f"[mcpelevator] stable run ended: {failure}; "
                            f"starting a new activation in {delay:g}s"
                        )
                        await asyncio.sleep(delay)
                        if self._stopping:
                            return
                        self.last_error = failure
                        self.startup_status = None
                        self._set_state("unhealthy")
                        return
                    raise _AttemptFailed(f"bridge exited before stable run rc={rc}")
                except _AttemptFailed as exc:
                    last_failure = str(exc)[:300]
                    # Stop advertising a dead bridge before process/container cleanup,
                    # which can take several seconds for a slow Docker daemon.
                    self._set_state("starting")
                    await self._cleanup_attempt()
                    if self._stopping:
                        return
                    if attempt >= max_attempts:
                        self.last_error = last_failure
                        self.startup_status = None
                        self._release_port()
                        self._set_state("failed")
                        self.logs.append(f"[mcpelevator] activation failed: {last_failure}")
                        return
                    delay = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
                    self._set_state("starting")
                    self._set_status(
                        "retry_wait",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        next_retry_at=utcnow() + timedelta(seconds=delay),
                        message=last_failure,
                    )
                    self.logs.append(
                        f"[mcpelevator] attempt {attempt}/{max_attempts} failed: "
                        f"{last_failure}; retrying in {delay:g}s"
                    )
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            await self._cleanup_attempt()
            raise
        except Exception as exc:
            await self._cleanup_attempt()
            if not self._stopping:
                self.last_error = str(exc)[:300] or last_failure
                self.startup_status = None
                self._release_port()
                self._set_state("failed")

    async def _run_attempt(self, attempt: int, max_attempts: int) -> tuple[bool, int]:
        self.last_error = None
        self.tools = []
        if self.spec.setup_script:
            await self._run_setup(attempt, max_attempts)
        await self._launch_bridge(attempt, max_attempts)
        await self._await_ready(attempt, max_attempts)

        self.last_health = utcnow()
        self.restart_count = 0
        self.startup_status = None
        self._set_state("running")
        self.logs.append(f"[mcpelevator] ready - {len(self.tools)} tool(s)")
        return await self._wait_for_bridge_exit(get_settings().restart_stable_s)

    async def _run_setup(self, attempt: int, max_attempts: int) -> None:
        timeout = float(get_settings().start_timeout_s)
        self._set_state("starting")
        self._set_status(
            "setup",
            attempt=attempt,
            max_attempts=max_attempts,
            deadline_at=utcnow() + timedelta(seconds=timeout),
        )
        self.logs.append(f"[mcpelevator] attempt {attempt}/{max_attempts}: setup")
        try:
            async with asyncio.timeout(timeout):
                self.setup_proc = await asyncio.create_subprocess_exec(
                    "/bin/sh",
                    "-e",
                    "-c",
                    self.spec.setup_script,
                    cwd=self._effective_child_cwd(),
                    env=self._effective_child_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                assert self.setup_proc.stdout is not None
                self._setup_log_task = asyncio.create_task(
                    self._pump_stream(self.setup_proc.stdout)
                )
                await self.setup_proc.wait()
                # A background descendant can keep the inherited stdout pipe open
                # after the shell exits. Log draining is part of the setup deadline.
                await self._setup_log_task
        except asyncio.TimeoutError as exc:
            await self._terminate(self.setup_proc, include_descendants=True)
            await self._finish_log_task(self._setup_log_task)
            self.setup_proc = None
            self._setup_log_task = None
            raise _AttemptFailed(f"setup timed out after {timeout:g}s") from exc
        except (OSError, ValueError) as exc:
            raise _AttemptFailed(f"setup could not start: {exc}") from exc
        rc = self.setup_proc.returncode
        self.setup_proc = None
        self._setup_log_task = None
        if rc != 0:
            raise _AttemptFailed(f"setup exited with code {rc}")

    async def _launch_bridge(self, attempt: int, max_attempts: int) -> None:
        settings = get_settings()
        assert self.port is not None
        self._set_status("bridge", attempt=attempt, max_attempts=max_attempts)
        self.logs.append(f"[mcpelevator] attempt {attempt}/{max_attempts}: bridge")
        payload = self._bridge_payload()
        env = self._bridge_env(payload)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-m",
                "app.bridge.host",
                cwd=str(settings.backend_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise _AttemptFailed(f"bridge could not start: {exc}") from exc
        self.logs.append(f"[mcpelevator] starting bridge pid={self.proc.pid} port={self.port}")
        assert self.proc.stdout is not None
        self._log_task = asyncio.create_task(self._pump_stream(self.proc.stdout))

    def _bridge_payload(self) -> dict:
        return {
            "command": self.spec.command,
            "args": self.spec.args,
            "env": self.spec.env,
            "cwd": self.spec.cwd,
            "transport": self.spec.transport,
            "minimal_env": self.spec.minimal_env,
            "oauth": self.spec.oauth,
            "name": self.name,
            **self.exposure,
        }

    async def _await_ready(self, attempt: int, max_attempts: int) -> None:
        settings = get_settings()
        timeout = float(settings.start_timeout_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        url = f"http://{self.host}:{self.port}/mcp"
        last_error = "readiness timeout"
        self._set_status(
            "readiness",
            attempt=attempt,
            max_attempts=max_attempts,
            deadline_at=utcnow() + timedelta(seconds=timeout),
        )
        self.logs.append(f"[mcpelevator] attempt {attempt}/{max_attempts}: readiness")

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _AttemptFailed(f"readiness timed out: {last_error}")
            assert self.proc is not None
            exit_task = asyncio.create_task(self.proc.wait())
            probe_task = asyncio.create_task(self._probe(url, remaining))
            try:
                done, _ = await asyncio.wait(
                    {exit_task, probe_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if exit_task in done:
                    raise _AttemptFailed(
                        f"bridge exited during readiness rc={exit_task.result()}"
                    )
                try:
                    tools = probe_task.result()
                except Exception as exc:
                    last_error = str(exc)[:300]
                    tools = None
            finally:
                for task in (exit_task, probe_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(exit_task, probe_task, return_exceptions=True)
            if tools is None:
                self._set_status(
                    "readiness",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    deadline_at=utcnow()
                    + timedelta(seconds=max(0.0, deadline - loop.time())),
                    message=last_error,
                )
                await asyncio.sleep(
                    min(_READINESS_RETRY_SECONDS, max(0.0, deadline - loop.time()))
                )
                continue
            self.tools = [tool_summary(tool) for tool in tools]
            return

    async def _probe(self, url: str, timeout: float):
        async with asyncio.timeout(timeout):
            async with Client(url, timeout=timeout) as client:
                return await client.list_tools()

    async def _wait_for_bridge_exit(self, stable_s: float) -> tuple[bool, int]:
        assert self.proc is not None
        exit_task = asyncio.create_task(self.proc.wait())
        if stable_s <= 0:
            self.logs.append("[mcpelevator] stable run reached; retry budget restored")
            rc = await exit_task
            await self._await_task(self._log_task)
            return True, rc

        stable_task = asyncio.create_task(asyncio.sleep(stable_s))
        try:
            done, _ = await asyncio.wait(
                {exit_task, stable_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if exit_task in done:
                stable_task.cancel()
                await self._await_task(stable_task)
                await self._await_task(self._log_task)
                return False, exit_task.result()
            self.logs.append("[mcpelevator] stable run reached; retry budget restored")
            self.restart_count = 0
            rc = await exit_task
            await self._await_task(self._log_task)
            return True, rc
        finally:
            if not exit_task.done():
                exit_task.cancel()
            if not stable_task.done():
                stable_task.cancel()

    async def _cleanup_attempt(self) -> None:
        had_bridge = self.proc is not None
        await self._terminate(self.setup_proc, include_descendants=True)
        await self._terminate(self.proc)
        await self._finish_log_task(self._setup_log_task)
        await self._finish_log_task(self._log_task)
        self.setup_proc = None
        self.proc = None
        self._setup_log_task = None
        self._log_task = None
        if self.runner == "docker" and had_bridge:
            await self._reap_container()

    async def _terminate(
        self,
        proc: Optional[asyncio.subprocess.Process],
        *,
        include_descendants: bool = False,
    ) -> None:
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5)
        else:
            if include_descendants:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGTERM)
            await proc.wait()
        if include_descendants:
            # The shell may already be reaped while a background child keeps the
            # process group alive and ignores TERM. Cancellation owns the whole group.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    async def _pump_stream(self, stream: asyncio.StreamReader) -> None:
        async for raw in stream:
            self.logs.append(raw.decode(errors="replace").rstrip("\n"))

    @staticmethod
    async def _await_task(task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def _finish_log_task(task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _bridge_env(self, payload: dict) -> dict[str, str]:
        settings = get_settings()
        assert self.port is not None
        return {
            **os.environ,
            "MCPE_BRIDGE_SPEC": json.dumps(payload),
            "MCPE_BRIDGE_HOST": self.host,
            "MCPE_BRIDGE_PORT": str(self.port),
            "MCPE_DATA_DIR": str(settings.data_dir.resolve()),
            "PYTHONPATH": str(settings.backend_root),
        }

    def _effective_child_env(self) -> dict[str, str]:
        # Setup is local-runner only, so it receives the same bridge environment and
        # server overrides that the later stdio child starts with.
        return {**self._bridge_env(self._bridge_payload()), **self.spec.env}

    def _effective_child_cwd(self) -> str:
        settings = get_settings()
        if not self.spec.cwd:
            return str(settings.backend_root)
        cwd = Path(self.spec.cwd)
        if not cwd.is_absolute():
            cwd = settings.backend_root / cwd
        return str(cwd)

    def _set_status(
        self,
        phase: str,
        *,
        attempt: int,
        max_attempts: int,
        deadline_at: Optional[datetime] = None,
        next_retry_at: Optional[datetime] = None,
        message: Optional[str] = None,
    ) -> None:
        self.startup_status = StartupSnapshot(
            phase=phase,
            attempt=attempt,
            max_attempts=max_attempts,
            activation_started_at=self._activation_started_at,
            deadline_at=deadline_at,
            next_retry_at=next_retry_at,
            message=message,
        )
        self._notify()

    def _set_state(self, state: str) -> None:
        self.state = state
        self._notify()

    def _notify(self) -> None:
        if self._on_state_change is not None:
            with contextlib.suppress(Exception):
                self._on_state_change()

    def _release_port(self) -> None:
        if self._port_released:
            return
        port = self.port
        self.port = None
        self._port_released = True
        if port is not None and self._release_port_callback is not None:
            self._release_port_callback(port)

    async def _reap_container(self) -> None:
        label = server_label(self.id)
        try:
            listed = await asyncio.create_subprocess_exec(
                DOCKER_BIN,
                "ps",
                "-aq",
                "--filter",
                f"label={label}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            return
        try:
            out, _ = await asyncio.wait_for(listed.communicate(), timeout=8)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                listed.kill()
            await listed.wait()
            return
        ids = out.decode(errors="replace").split()
        if not ids:
            return
        try:
            rm = await asyncio.create_subprocess_exec(
                DOCKER_BIN,
                "rm",
                "-f",
                *ids,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            return
        try:
            await asyncio.wait_for(rm.wait(), timeout=8)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                rm.kill()
            await rm.wait()

    @property
    def pid(self) -> Optional[int]:
        if self.proc is None or self.proc.returncode is not None:
            return None
        return self.proc.pid

    def is_dead(self) -> bool:
        return self.proc is None or self.proc.returncode is not None
