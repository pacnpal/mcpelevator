"""Live process state for one Server activation."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastmcp import Client

from app.bridge.host import UPSTREAM_META_KEY
from app.bridge.spec import (
    BRIDGE_SPEC_ENV_KEY,
    BRIDGE_SPEC_MAX_BYTES,
    bridge_payload,
    oversize,
    serialize,
)
from app.config import (
    get_settings,
    is_control_plane_env_var,
    is_control_plane_secret_env_var,
)
from app.db.models import Server, utcnow
from app import sealed
from app.runners import build_spec
from app.runners import cache as server_cache
from app.runners import repo as repo_checkout
from app.runners.base import ProcessSpec
from app.runners.docker import DOCKER_BIN, server_label
from app.supervisor.hints import startup_hint
from app.supervisor.logbuffer import LogBuffer


logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0)
_READINESS_RETRY_SECONDS = 2.0
# Lines of the server's own log quoted in the standard log when an activation fails
# terminally or a running bridge dies, so the reason is there without opening the UI.
_FAILURE_TAIL_LINES = 20
_FAILURE_TAIL_LINE_CHARS = 500
# A health check's output kept for the server's log (its last lines) and quoted into
# the failure (about this many characters).
_HEALTH_OUTPUT_LINES = 40
_HEALTH_OUTPUT_LINE_CHARS = 500
_HEALTH_FAILURE_CHARS = 280

# Prepended to every setup / checkout shell. A setup that installs system packages (an
# ``apt-get install``, ``playwright install --with-deps``) and is stopped mid-run (a
# disable, a restart, its own deadline) leaves dpkg's journal half-applied, and from then
# on every apt call refuses with "dpkg was interrupted, you must manually run 'dpkg
# --configure -a'" — so every later attempt fails the same way until someone repairs it
# by hand. Only when dpkg says it was interrupted (a pending journal in
# /var/lib/dpkg/updates, or ``dpkg --audit`` reporting half-done packages), only as root,
# and only the repair apt itself prescribes: ``dpkg --configure -a`` finishes what was
# unpacked and removes nothing. A failed repair is reported and the script runs anyway,
# so its own error stays the one the operator sees.
DPKG_REPAIR_PREAMBLE = r'''if [ "$(id -u)" = 0 ] && command -v dpkg >/dev/null 2>&1 \
  && { [ -n "$(ls -A /var/lib/dpkg/updates 2>/dev/null)" ] || [ -n "$(dpkg --audit 2>/dev/null)" ]; }; then
  echo "[mcpelevator] dpkg was interrupted by an earlier run; repairing with dpkg --configure -a"
  DEBIAN_FRONTEND=noninteractive dpkg --configure -a \
    || echo "[mcpelevator] dpkg repair failed; run 'dpkg --configure -a' and 'apt-get -f install' in the container"
fi
'''


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


@dataclass(frozen=True)
class HealthCheck:
    """A server's health check (issue #225), snapshotted from its row: a ``tool`` called
    through the bridge with ``arguments``, or a shell ``command`` (local runners). It runs
    at readiness, and every ``interval_s`` seconds while the unit runs when that is set.
    Outside config_hash: the supervisor hands a running unit the current one each pass
    (:meth:`ServerUnit.tick_health_check`), so an edit lands without a restart."""

    tool: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    command: Optional[str] = None
    interval_s: Optional[int] = None

    @classmethod
    def from_server(cls, server) -> Optional["HealthCheck"]:
        tool = (getattr(server, "health_check_tool", None) or "").strip() or None
        command = getattr(server, "health_check_command", None) or None
        if command is not None and not command.strip():
            command = None
        if tool is None and command is None:
            return None
        interval = getattr(server, "health_check_interval_s", None)
        arguments = getattr(server, "health_check_arguments", None) or {}
        return cls(
            tool=tool,
            arguments=dict(arguments) if tool else {},
            # The service refuses both at once; should a row hold both, the tool wins.
            command=None if tool else command,
            interval_s=interval if isinstance(interval, int) and interval > 0 else None,
        )

    @property
    def label(self) -> str:
        return f"tool {self.tool}" if self.tool else "command"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _tool_result_text(result) -> str:
    """The readable part of a tool result: its text blocks, else its structured content."""
    parts = [
        block.text
        for block in (result.content or [])
        if isinstance(getattr(block, "text", None), str)
    ]
    if not parts and result.structured_content is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(json.dumps(result.structured_content))
    return "\n".join(parts)


def tool_summary(tool) -> dict:
    """One cached-tool entry for the UI, from a probed ``mcp.types.Tool``.

    ``input_schema`` is the tool's full JSON input schema — the playground builds
    its argument form from it, so it rides along in the cache rather than needing
    a live round-trip to the bridge on every page view.

    ``upstream_name`` is the tool's identity BEFORE any operator rename, stamped into
    ``_meta`` by the bridge's tool transform (see ``bridge.host``). Absent when the tool
    isn't renamed — then ``name`` already IS the upstream name. The UI keys its per-tool
    rows off this, so it never has to infer identity by reversing the rename map (an
    exposed name isn't unique, so that inference can misidentify a tool). Trustworthy
    because the bridge strips the key from every upstream tool before stamping its own —
    an upstream can't forge an identity the UI would then key policy off."""
    summary = {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema or {},
        "has_output_schema": tool.output_schema is not None,
    }
    upstream = (getattr(tool, "meta", None) or {}).get(UPSTREAM_META_KEY)
    if isinstance(upstream, dict) and isinstance(upstream.get("name"), str):
        summary["upstream_name"] = upstream["name"]
    return summary


def _with_checkout_cwd(spec: ProcessSpec, server_id: str) -> ProcessSpec:
    """A repository server runs inside its checkout: ``cwd`` blank = the checkout root, a
    relative one is taken inside it (e.g. a monorepo package dir), an absolute one stands.
    Resolved here rather than in the runner so builders stay pure (no data-dir global)."""
    if not spec.repo:
        return spec
    root = repo_checkout.checkout_dir(get_settings().data_dir, server_id)
    cwd = root / spec.cwd if spec.cwd else root
    return replace(spec, cwd=str(cwd))


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
        self.pin_mcp1 = bool(server.pin_mcp1)
        self.spec = _with_checkout_cwd(build_spec(server), server.id)
        # A local child's own cache directory (app.runners.cache), resolved here for the
        # same reason as the checkout cwd: the builders never see the data dir.
        self.cache_dir: Optional[Path] = (
            server_cache.cache_dir(get_settings().data_dir, server.id)
            if server_cache.has_cache(server.runner)
            else None
        )
        self.exposure = {"mcp_http": server.mcp_http, "rest_openapi": server.rest_openapi}
        # The plaintext of this server's secret env (headers too, for remote/openapi), so a
        # failure tail quoted into the standard log can't carry one: debug.scrub only
        # recognizes labelled secrets, and a child may echo a bare value. Longest first, so
        # a value containing another is hidden whole. Very short values are skipped —
        # replacing them would mangle the output and they aren't credentials anyway.
        stored = server.env or {}
        self._secret_values = sorted(
            {
                value
                for key, value in self.spec.env.items()
                if sealed.is_sealed(stored.get(key)) and len(value) >= 4
            },
            key=len,
            reverse=True,
        )
        # Per-server startup deadlines; None = the instance default (read per attempt).
        self.start_timeout_s: Optional[int] = getattr(server, "start_timeout_s", None)
        self.repo_build_timeout_s: Optional[int] = getattr(server, "repo_build_timeout_s", None)
        # The health check; refreshed by the supervisor each pass (tick_health_check).
        self.health_check: Optional[HealthCheck] = HealthCheck.from_server(server)
        # True while the bridge runs but its periodic health check fails: the unit reads
        # "unhealthy" yet is not a crashed one, so the supervisor leaves it be (no restart)
        # and keeps checking it until a check passes. Cleared by any other state change.
        self.health_failing = False

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
        self._health_task: Optional[asyncio.Task[None]] = None
        self._health_wakeup: Optional[asyncio.TimerHandle] = None
        # Loop time the periodic clock counts from: reaching running, then each check's end.
        self._health_last_at: Optional[float] = None
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
        self._health_last_at = None
        self._activation_started_at = activation_started_at or utcnow()
        self._set_state("starting")
        self._set_status("queued", attempt=1, max_attempts=max_attempts)
        self.logs.append(f"[mcpelevator] activation started; max attempts={max_attempts}")
        logger.info(
            "server %s: starting (runner %s, up to %d attempt(s), start timeout %gs)",
            self.slug, self.runner, max_attempts, self._start_timeout(),
        )
        self._activation_task = asyncio.create_task(self._run_activation(max_attempts))

    def _start_timeout(self) -> float:
        return float(self.start_timeout_s or get_settings().start_timeout_s)

    def _repo_build_timeout(self) -> float:
        return float(self.repo_build_timeout_s or get_settings().repo_build_timeout_s)

    def _failure_tail(self) -> str:
        """The last lines of this server's log, indented for the standard log."""
        lines = [
            self._redact(line)
            for line in list(self.logs.lines)[-_FAILURE_TAIL_LINES:]
            if line.strip()
        ]
        if not lines:
            return ""
        body = "\n".join(
            "    " + (line if len(line) <= _FAILURE_TAIL_LINE_CHARS
                      else line[:_FAILURE_TAIL_LINE_CHARS] + "…")
            for line in lines
        )
        return f"\n  last {len(lines)} log line(s):\n{body}"

    def _redact(self, line: str) -> str:
        for value in self._secret_values:
            line = line.replace(value, sealed.HIDDEN)
        return line

    async def stop(self) -> None:
        was = self.state
        if self.startup_status is not None and self.startup_status.phase in ("setup", "checkout"):
            logger.warning(
                "server %s: stopping during %s; the script is interrupted (an interrupted "
                "package install is repaired on the next start)",
                self.slug, self.startup_status.phase,
            )
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
        if was != "stopped":
            logger.info("server %s: stopped", self.slug)

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
                        logger.warning(
                            "server %s: bridge exited while running (%s); "
                            "starting a new activation in %gs%s",
                            self.slug, failure, delay, self._failure_tail(),
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
                        self.last_error = self._with_hint(last_failure)
                        self.startup_status = None
                        self._release_port()
                        logger.error(
                            "server %s: failed to start after %d attempt(s): %s%s",
                            self.slug, attempt, self.last_error, self._failure_tail(),
                        )
                        self._set_state("failed")
                        self.logs.append(f"[mcpelevator] activation failed: {self.last_error}")
                        return
                    delay = _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]
                    # Surface the failure on the server itself, not only in the retry
                    # status: a server retrying a broken setup must not read as clean.
                    # Cleared when an attempt reaches running.
                    self.last_error = (
                        f"attempt {attempt}/{max_attempts} failed: {last_failure}"
                    )
                    logger.warning(
                        "server %s: attempt %d/%d failed: %s; retrying in %gs",
                        self.slug, attempt, max_attempts, last_failure, delay,
                    )
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
            logger.exception("server %s: activation crashed", self.slug)
            await self._cleanup_attempt()
            if not self._stopping:
                self.last_error = self._with_hint(str(exc)[:300] or last_failure)
                self.startup_status = None
                self._release_port()
                self._set_state("failed")

    def _with_hint(self, failure: str) -> str:
        # Terminal failures only: mid-retry statuses stay raw, since the next
        # attempt may succeed and a recommendation would just be noise. Setup
        # failures carry their phase in the message (every _run_setup failure
        # string starts with "setup ") — that phase runs in its own shell, so
        # launch-argv remedies don't apply to it. Best-effort decoration over
        # UNTRUSTED child output: any failure here must never break failure
        # reporting itself (an exception on this path would leave the unit stuck
        # in "starting" with its port unreleased).
        if failure.startswith("checkout "):
            # The clone/build log is right above in the server's logs; the launch and
            # setup remedies startup_hint knows don't apply to it.
            return failure
        try:
            hint = startup_hint(
                self.logs.lines,
                self.runner,
                setup_failed=failure.startswith("setup "),
                command=self.spec.command,
                args=self.spec.args,
                pinned=self.pin_mcp1,
            )
        except Exception:
            return failure
        return f"{failure} (hint: {hint})" if hint else failure

    async def _run_attempt(self, attempt: int, max_attempts: int) -> tuple[bool, int]:
        # last_error keeps the previous attempt's failure until this one reaches running.
        self.tools = []
        if self.cache_dir is not None:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Best effort: tools create their cache dir themselves; this only spares
                # the ones that don't.
                self.logs.append(f"[mcpelevator] cache directory not created: {exc}")
        if self.spec.repo:
            await self._run_checkout(attempt, max_attempts)
        if self.spec.setup_script:
            await self._run_setup(attempt, max_attempts)
        await self._launch_bridge(attempt, max_attempts)
        await self._await_ready(attempt, max_attempts)

        self._health_last_at = asyncio.get_running_loop().time()
        self.last_health = utcnow()
        self.restart_count = 0
        self.last_error = None
        self.startup_status = None
        self._set_state("running")
        self.logs.append(f"[mcpelevator] ready - {len(self.tools)} tool(s)")
        logger.info(
            "server %s: running on port %s with %d tool(s) (attempt %d/%d)",
            self.slug, self.port, len(self.tools), attempt, max_attempts,
        )
        return await self._wait_for_bridge_exit(get_settings().restart_stable_s)

    async def _run_checkout(self, attempt: int, max_attempts: int) -> None:
        """Clone/fetch the server's repository and run its post-clone script when the
        commit or the script changed (app.runners.repo). Its own phase and deadline: a
        first build routinely outlasts start_timeout_s."""
        settings = get_settings()
        repo = self.spec.repo
        assert repo is not None
        script = repo_checkout.checkout_script(
            repo["url"],
            repo["ref"],
            repo["post_clone_script"],
            repo_checkout.checkout_dir(settings.data_dir, self.id),
            repo_checkout.stamp_path(settings.data_dir, self.id),
        )
        # The checkout's parent must exist before the shell can cd anywhere near it.
        repo_checkout.checkout_dir(settings.data_dir, self.id).parent.mkdir(
            parents=True, exist_ok=True
        )
        await self._run_shell(
            "checkout",
            script,
            cwd=str(repo_checkout.checkout_dir(settings.data_dir, self.id).parent),
            timeout=self._repo_build_timeout(),
            attempt=attempt,
            max_attempts=max_attempts,
        )

    async def _run_setup(self, attempt: int, max_attempts: int) -> None:
        await self._run_shell(
            "setup",
            self.spec.setup_script,
            cwd=self._effective_child_cwd(),
            timeout=self._start_timeout(),
            attempt=attempt,
            max_attempts=max_attempts,
        )

    async def _run_shell(
        self,
        phase: str,
        script: str,
        *,
        cwd: str,
        timeout: float,
        attempt: int,
        max_attempts: int,
    ) -> None:
        """Run one pre-launch phase (``setup`` or ``checkout``) as ``sh -e -c`` with the
        child's scrubbed env. Every failure string starts with the phase name, which is how
        ``_with_hint`` tells which phase failed."""
        self._set_state("starting")
        self._set_status(
            phase,
            attempt=attempt,
            max_attempts=max_attempts,
            deadline_at=utcnow() + timedelta(seconds=timeout),
        )
        self.logs.append(f"[mcpelevator] attempt {attempt}/{max_attempts}: {phase}")
        try:
            async with asyncio.timeout(timeout):
                self.setup_proc = await asyncio.create_subprocess_exec(
                    "/bin/sh",
                    "-e",
                    "-c",
                    DPKG_REPAIR_PREAMBLE + script,
                    cwd=cwd,
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
            raise _AttemptFailed(f"{phase} timed out after {timeout:g}s") from exc
        except (OSError, ValueError) as exc:
            raise _AttemptFailed(f"{phase} could not start: {exc}") from exc
        rc = self.setup_proc.returncode
        self.setup_proc = None
        self._setup_log_task = None
        if rc != 0:
            raise _AttemptFailed(f"{phase} exited with code {rc}")

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
                stdin=asyncio.subprocess.PIPE if self.spec.openapi_document else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise _AttemptFailed(f"bridge could not start: {exc}") from exc
        self.logs.append(f"[mcpelevator] starting bridge pid={self.proc.pid} port={self.port}")
        assert self.proc.stdout is not None
        # Pumping first: the bridge may log before it reads stdin, and a full stdout pipe
        # would then stall both sides of the write below.
        self._log_task = asyncio.create_task(self._pump_stream(self.proc.stdout))
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.write(self.spec.openapi_document.encode())
                await asyncio.wait_for(self.proc.stdin.drain(), self._start_timeout())
                self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the bridge already exited; readiness reports it with its log
            except asyncio.TimeoutError as exc:
                raise _AttemptFailed("bridge did not read its OpenAPI document") from exc

    def _bridge_payload(self) -> dict:
        return bridge_payload(self.spec, self.name, **self.exposure)

    async def _await_ready(self, attempt: int, max_attempts: int) -> None:
        timeout = self._start_timeout()
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
            await self._readiness_health_check(attempt, max_attempts, deadline)
            return

    async def _readiness_health_check(
        self, attempt: int, max_attempts: int, deadline: float
    ) -> None:
        """Run the server's health check once the bridge answers, inside the same startup
        deadline. A failure fails the attempt, so it takes the ordinary retry/backoff path
        and its reason reaches ``last_error``, the container log and ``startup_hint`` (the
        output is logged under this phase's marker)."""
        check = self.health_check
        if check is None:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        self._set_status(
            "health",
            attempt=attempt,
            max_attempts=max_attempts,
            deadline_at=utcnow() + timedelta(seconds=max(0.0, remaining)),
        )
        self.logs.append(f"[mcpelevator] attempt {attempt}/{max_attempts}: health")
        if remaining <= 0:
            raise _AttemptFailed(
                "health check failed: no time left before the startup deadline"
            )
        failure, output = await self._health_check_once(check, remaining)
        self._log_health_output(output)
        if failure is not None:
            raise _AttemptFailed(f"health check failed: {failure}")
        self.logs.append(f"[mcpelevator] health check passed ({check.label})")

    def _log_health_output(self, output: list[str]) -> None:
        # Prefixed, so a line of untrusted output can never pose as one of the unit's
        # own phase markers (hints.startup_hint sections the log by them).
        for line in output:
            if len(line) > _HEALTH_OUTPUT_LINE_CHARS:
                line = line[:_HEALTH_OUTPUT_LINE_CHARS] + "…"
            self.logs.append(f"[health] {line}")

    async def _health_check_once(
        self, check: HealthCheck, timeout: float
    ) -> tuple[Optional[str], list[str]]:
        """Run ``check`` once: ``(None, output)`` when it passes, else ``(why, output)``.
        Never raises (but for cancellation): any error is the check failing. ``why`` quotes
        the output and lands in ``last_error`` and the container log, so the server's
        secret env values are hidden in it (the log buffer, like setup output, keeps the
        output as printed)."""
        try:
            if check.tool is not None:
                failure, output = await self._health_tool_call(check, timeout)
            else:
                failure, output = await self._health_command(check.command or "", timeout)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            return f"timed out after {timeout:g}s", []
        except Exception as exc:
            reason = _one_line(str(exc)) or type(exc).__name__
            failure, output = reason[:_HEALTH_FAILURE_CHARS], [reason]
        return (self._redact(failure) if failure is not None else None), output

    async def _health_tool_call(
        self, check: HealthCheck, timeout: float
    ) -> tuple[Optional[str], list[str]]:
        """Call the check's tool through the bridge's own loopback /mcp — the surface
        every client reaches, so a hidden or renamed tool behaves as it does for them. Not
        through the /s proxy: it is neither usage nor traffic that keeps a server awake."""
        if self.port is None:
            return "the bridge is not running", []
        url = f"http://{self.host}:{self.port}/mcp"
        async with asyncio.timeout(timeout):
            async with Client(url, timeout=timeout) as client:
                result = await client.call_tool(
                    check.tool, dict(check.arguments), timeout=timeout, raise_on_error=False
                )
        text = _tool_result_text(result)
        output = text.splitlines()[-_HEALTH_OUTPUT_LINES:]
        if result.is_error:
            reason = _one_line(text) or f"tool {check.tool} returned an error"
            return reason[:_HEALTH_FAILURE_CHARS], output
        return None, output

    async def _health_command(
        self, command: str, timeout: float
    ) -> tuple[Optional[str], list[str]]:
        """Run the check's shell command like the setup script: ``sh -e -c`` behind the
        dpkg preamble, with the child's scrubbed env and cwd, in its own process group
        (torn down whole on a timeout or a stop). A non-zero exit fails."""
        output: collections.deque[str] = collections.deque(maxlen=_HEALTH_OUTPUT_LINES)
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-e",
            "-c",
            DPKG_REPAIR_PREAMBLE + command,
            cwd=self._effective_child_cwd(),
            env=self._effective_child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

        async def pump() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                output.append(raw.decode(errors="replace").rstrip("\n"))

        reader = asyncio.create_task(pump())
        try:
            try:
                async with asyncio.timeout(timeout):
                    rc = await proc.wait()
                    await reader
            except (asyncio.TimeoutError, TimeoutError):
                return f"timed out after {timeout:g}s", list(output)
        finally:
            await self._terminate(proc, include_descendants=True)
            await self._finish_log_task(reader)
        if rc == 0:
            return None, list(output)
        # sh -e stops at the failing command, so the end of the output says why.
        tail = _one_line(" ".join(output))
        if len(tail) > _HEALTH_FAILURE_CHARS:
            tail = "…" + tail[-_HEALTH_FAILURE_CHARS:]
        return (f"exited with code {rc}" + (f": {tail}" if tail else "")), list(output)

    # --- periodic health check (driven by the supervisor) ------------------ #

    def _bridge_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.port is not None

    def _health_checkable(self) -> bool:
        return self._bridge_alive() and (
            self.state == "running" or (self.state == "unhealthy" and self.health_failing)
        )

    def tick_health_check(self, check: Optional[HealthCheck]) -> bool:
        """Called by the supervisor on every reconcile pass with the server's current
        health check. Starts a periodic check when one is due — only against a live bridge
        that is running (or failing its check), never during startup, while stopping, or
        for an idle server (which has no unit). Returns True when a check was started.

        A check removed while the unit is failing it returns the unit to running: nothing
        says it is unhealthy any more."""
        self.health_check = check
        interval = check.interval_s if check is not None else None
        if interval is None:
            if self.health_failing and not self._stopping and self._health_checkable():
                self._health_recovered("health check removed")
            return False
        if self._stopping or (self._health_task is not None and not self._health_task.done()):
            return False
        if not self._health_checkable():
            return False
        if self._health_last_at is None:
            return False
        loop = asyncio.get_running_loop()
        due_at = self._health_last_at + interval
        if loop.time() < due_at:
            # Not yet: ask for a pass when it is, rather than waiting out the rest of the
            # supervisor's interval on top of the check's own.
            wakeup = self._health_wakeup
            if wakeup is None or wakeup.cancelled() or wakeup.when() > due_at + 0.5 or (
                wakeup.when() <= loop.time()
            ):
                if wakeup is not None:
                    wakeup.cancel()
                self._health_wakeup = loop.call_at(due_at, self._notify)
            return False
        self._health_task = asyncio.create_task(self._periodic_health_check(check))
        return True

    async def _periodic_health_check(self, check: HealthCheck) -> None:
        proc = self.proc
        loop = asyncio.get_running_loop()
        try:
            failure, output = await self._health_check_once(check, self._start_timeout())
        finally:
            self._health_last_at = loop.time()
            # A pass now schedules the next check (tick_health_check arms its wakeup).
            self._notify()
        # The world may have moved on while the check ran: the bridge exited (the
        # activation handles that), the unit is stopping, or a new process replaced it.
        if self._stopping or self.proc is not proc or not self._health_checkable():
            return
        if failure is None:
            self.last_health = utcnow()
            if self.health_failing:
                self._health_recovered(f"health check passed ({check.label})")
            return
        detail = f"health check failed: {failure}"[:300]
        if not self.health_failing:
            # The output once, on the transition; a server that stays unhealthy logs one
            # line per check rather than repeating it.
            self._log_health_output(output)
        self.logs.append(f"[mcpelevator] periodic {detail}")
        error = self._with_hint(detail)
        if not self.health_failing:
            logger.warning(
                "server %s: %s; marked unhealthy until a check passes", self.slug, error
            )
        self.last_error = error
        self._set_state("unhealthy", health_failing=True)

    def _health_recovered(self, why: str) -> None:
        self.last_error = None
        self.last_health = utcnow()
        self.logs.append(f"[mcpelevator] {why}; back to running")
        logger.info("server %s: %s; running again", self.slug, why)
        self._set_state("running")

    async def _cancel_health_check(self) -> None:
        if self._health_wakeup is not None:
            self._health_wakeup.cancel()
            self._health_wakeup = None
        task = self._health_task
        self._health_task = None
        if task is not None and not task.done():
            task.cancel()
        await self._await_task(task)

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
        await self._cancel_health_check()
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
        # The bridge is trusted mcpelevator code and never needs the break-glass admin token, so
        # keep it OUT of the bridge's environment: an untrusted local child would otherwise recover
        # it from the immediate parent's ``/proc/<ppid>/environ`` on a same-UID host despite the
        # child's own env being scrubbed. (The control-plane process's own /proc still exposes an
        # operator-supplied admin token — a same-UID limitation; see docs/security.md.)
        inherited = {k: v for k, v in os.environ.items() if not is_control_plane_secret_env_var(k)}
        spec = serialize(payload)
        # The registry refuses an oversized spec at write time; this is the backstop for a
        # row that predates that check or grew through a path that skipped it.
        size = oversize(spec)
        if size is not None:
            raise _AttemptFailed(
                f"launch spec is too large to pass to the bridge "
                f"({size} bytes; max {BRIDGE_SPEC_MAX_BYTES}) — "
                f"shorten the server's env, setup script, or tool overrides"
            )
        env = {
            **inherited,
            BRIDGE_SPEC_ENV_KEY: spec,
            "MCPE_BRIDGE_HOST": self.host,
            "MCPE_BRIDGE_PORT": str(self.port),
            "MCPE_DATA_DIR": str(settings.data_dir.resolve()),
            "PYTHONPATH": str(settings.backend_root),
        }
        # Never inherited: only a unit with a cache hands the bridge one.
        env.pop(server_cache.BRIDGE_ENV_KEY, None)
        if self.cache_dir is not None:
            env[server_cache.BRIDGE_ENV_KEY] = str(self.cache_dir)
        return env

    def _effective_child_env(self) -> dict[str, str]:
        # Setup is local-runner only and runs as ``/bin/sh -e -c <script>``, so it gets the SAME
        # scrubbed environment the later stdio child starts with: the inherited env minus the control
        # plane's own ``MCPE_*`` secrets, plus the server's declared vars. It must not see the
        # elevator's admin token / signing keys / the bridge spec even though it's operator code.
        base = {k: v for k, v in os.environ.items() if not is_control_plane_env_var(k)}
        if self.spec.repo:
            # Same as the launch (bridge.host._child_env): the checkout's own .venv only.
            for key in repo_checkout.FOREIGN_ENV:
                base.pop(key, None)
        # Same cache as the launched child, so a setup script's `npx playwright install` or
        # model download lands where the server will look. The server's env still wins.
        cache = server_cache.cache_env(self.cache_dir) if self.cache_dir is not None else {}
        return {**base, **cache, **self.spec.env}

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

    def _set_state(self, state: str, *, health_failing: bool = False) -> None:
        self.state = state
        self.health_failing = health_failing
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
