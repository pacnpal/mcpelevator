"""Supervisor — the single owner of runtime state.

API handlers never spawn or kill processes. They write desired state (SQLite) and
``nudge()`` this reconciler, which converges actual -> desired (Kubernetes-style).
That indirection is what makes the system idempotent and deterministic: re-running
``reconcile_once`` with unchanged desired state is a no-op, and ``config_hash`` is
the anchor that decides when a restart is actually needed.

The supervisor starts desired Servers, stops undesired ones, replaces activations
on launch-config changes, and persists observed state. Each ServerUnit owns its
bounded retry policy; this reconciler replaces it after a Stable run ends.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Awaitable, Callable, Optional

from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine, repo
from app.db.models import Server, utcnow
from app.registry import service as registry_service
from app.registry import settings as runtime_settings
from app.runners.docker import DOCKER_BIN, LABEL_KEY
from app.supervisor.ports import PortAllocator
from app.supervisor.unit import ServerUnit

# `docker ps` Go-template that prints "<container-id> <server-id-label>" per line, so the
# boot sweep can keep only containers whose label value is a server in THIS instance's DB.
_PS_FORMAT = '{{.ID}} {{.Label "' + LABEL_KEY + '"}}'


async def _run_docker_capture(argv: list[str], *, timeout: float) -> Optional[str]:
    """Run a docker CLI command and return its stdout (decoded), or ``None`` on any failure.

    Best-effort and non-blocking (async subprocess, not ``subprocess.run``) so a slow/wedged
    daemon can't stall the caller. On timeout the child is killed AND awaited (no zombie).
    Returns ``None`` for a missing CLI, unreachable daemon, non-zero exit, or timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return out.decode(errors="replace")


class Supervisor:
    def __init__(self) -> None:
        settings = get_settings()
        self.ports = PortAllocator(settings.port_range_start, settings.port_range_end)
        self.max_running = settings.max_running
        self.interval = settings.health_interval_s
        self.units: dict[str, ServerUnit] = {}
        self._activation_requests: dict[str, datetime] = {}
        self._unit_lock = asyncio.Lock()
        self._nudge = asyncio.Event()
        self._stopping = False
        # Fired after each reconcile pass so a dependent (the group hub) can
        # converge on the new topology. The supervisor stays group-unaware.
        self.on_converged: Optional[Callable[[], Awaitable[None]]] = None

    # --- lookups (used by the reverse proxy + API) ----------------------- #

    def unit(self, server_id: str) -> Optional[ServerUnit]:
        return self.units.get(server_id)

    def unit_by_slug(self, slug: str) -> Optional[ServerUnit]:
        return next((u for u in self.units.values() if u.slug == slug), None)

    def endpoint(self, slug: str) -> Optional[tuple[str, int]]:
        u = self.unit_by_slug(slug)
        if u is not None and u.state == "running" and u.port is not None:
            return (u.host, u.port)
        return None

    def running_endpoints(self) -> list[tuple[str, str, str, int]]:
        """``(server_id, slug, host, port)`` for every unit currently running —
        the live topology the group hub mounts from."""
        return [
            (server_id, u.slug, u.host, u.port)
            for server_id, u in self.units.items()
            if u.state == "running" and u.port is not None
        ]

    def rename_slug(self, server_id: str, slug: str) -> None:
        """Update a live unit's routing slug in place (no restart).

        The slug is only a proxy routing key — the bridge subprocess doesn't use it
        and ``config_hash`` excludes it, so the reconciler won't re-derive the unit on
        a rename. Point the running unit at the new slug here so ``/s/<new>/`` resolves
        immediately; if the server isn't running this is a no-op (a later start
        snapshots the persisted slug)."""
        unit = self.units.get(server_id)
        if unit is not None:
            unit.slug = slug

    def activation_requested_at(self, server_id: str):
        return self._activation_requests.get(server_id)

    def request_activation(self, server_id: str) -> None:
        self._activation_requests[server_id] = utcnow()
        self.nudge()

    def cancel_activation_request(self, server_id: str) -> None:
        self._activation_requests.pop(server_id, None)

    # --- start/stop a single unit ---------------------------------------- #

    async def _start(self, server, *, activation_started_at=None) -> None:
        if sum(unit.port is not None for unit in self.units.values()) >= self.max_running:
            raise RuntimeError(f"max_running ({self.max_running}) reached")
        unit = ServerUnit(
            server,
            on_state_change=self.nudge,
            release_port=self.ports.release,
        )
        port = self.ports.allocate()
        self.units[server.id] = unit
        try:
            await unit.start(port, activation_started_at=activation_started_at)
        except BaseException:
            self.units.pop(server.id, None)
            self.ports.release(port)
            raise

    async def _stop(self, server_id: str) -> None:
        unit = self.units.pop(server_id, None)
        if unit is not None:
            await unit.stop()

    async def stop(self, server_id: str) -> None:
        """Public stop (e.g. API-driven delete). Steady state is still reconciled."""
        async with self._unit_lock:
            await self._stop(server_id)

    async def retry(self, server_id: str) -> bool:
        async with self._unit_lock:
            unit = self.units.get(server_id)
            if unit is not None and (
                unit.state not in ("failed", "unhealthy")
                or unit.startup_status is not None
            ):
                return False
            await self._stop(server_id)
        self.request_activation(server_id)
        return True

    # --- reconcile ------------------------------------------------------- #

    async def reconcile_once(self) -> None:
        async with self._unit_lock:
            await self._reconcile_once_unlocked()
        if self.on_converged is not None:
            try:
                await self.on_converged()
            except Exception as exc:  # a hub bug must never kill the reconcile loop
                print(f"[mcpelevator] post-reconcile hook error: {exc}")

    async def _reconcile_once_unlocked(self) -> None:
        # Snapshot desired state under a short-lived session, then RELEASE it before the slow
        # start/stop/reap I/O below. A single docker ``_stop`` can spend tens of seconds
        # reaping containers off a wedged daemon; holding this session open across the whole
        # sweep would pin a pooled connection (and, mid-transaction, the SQLite write lock) for
        # that entire time and stall API writers. Each runtime write below opens its own tiny
        # session (``_write_runtime``) so a lock is only ever held for the duration of one row.
        with Session(get_engine()) as session:
            docker_on = runtime_settings.docker_runner(session)
            servers = list(repo.list_servers(session))
            disabled_runtime_stale = {}
            for server in servers:
                if server.enabled:
                    continue
                runtime = repo.get_runtime(session, server.id)
                disabled_runtime_stale[server.id] = runtime is not None and (
                    runtime.state != "stopped"
                    or runtime.pid is not None
                    or runtime.port is not None
                    or runtime.last_error is not None
                    or runtime.restart_count != 0
                    or runtime.last_health is not None
                    or bool(runtime.tools)
                )

        desired: dict[str, Server] = {}
        disabled: list[Server] = []
        forbidden_docker: list[tuple[Server, str]] = []
        for sv in servers:
            if not sv.enabled:
                disabled.append(sv)
                self.cancel_activation_request(sv.id)
                continue
            # The docker runner is root-equivalent and opt-in: if it's off, an enabled docker
            # server must not run — collect it for a stop pass below (this also catches the
            # setting being turned off while a docker server is running, within one interval).
            if not docker_on and sv.runner == "docker":
                forbidden_docker.append(
                    (sv, "Docker runner is disabled (enable it in Settings)")
                )
                continue
            # A shell-wrapped docker CLI invocation on a passthrough runner can NEVER be routed
            # through the hardened docker runner (only real ``docker`` launchers canonicalize), so
            # starting it would spawn an ungated, unhardened container with the control plane's full
            # environment. Forbid it regardless of the docker_runner setting — even a legacy row that
            # predates this guard, and even while the runner is on.
            if registry_service.local_exec_invokes_docker(
                sv.runner, sv.command, sv.args
            ) or registry_service.setup_script_invokes_docker(
                sv.runner, sv.setup_script
            ):
                forbidden_docker.append(
                    (sv, "Docker CLI invocations require the docker runner")
                )
                continue
            desired[sv.id] = sv

        # gate: stop any docker unit now forbidden (runner off, or an unhardenable shell wrapper),
        # and surface why
        for sv, reason in forbidden_docker:
            self.cancel_activation_request(sv.id)
            if sv.id in self.units:
                await self._stop(sv.id)
            self._write_runtime(
                sv.id, state="failed", pid=None, port=None,
                last_error=reason, restart_count=0, last_health=None, tools=[],
            )

        # stop anything running that is no longer desired
        stopped_runtime: set[str] = set()
        for server_id in list(self.units):
            if server_id not in desired:
                self.cancel_activation_request(server_id)
                await self._stop(server_id)
                self._write_runtime(
                    server_id, state="stopped", pid=None, port=None,
                    last_error=None, restart_count=0, last_health=None, tools=[],
                )
                stopped_runtime.add(server_id)

        # A queued activation may have no unit yet. Disabling it still has to
        # converge a stale persisted runtime row to stopped.
        for sv in disabled:
            if sv.id not in stopped_runtime and disabled_runtime_stale.get(sv.id, False):
                self._write_runtime(
                    sv.id,
                    state="stopped",
                    pid=None,
                    port=None,
                    last_error=None,
                    restart_count=0,
                    last_health=None,
                    tools=[],
                )

        # start / restart desired
        for server_id, server in desired.items():
            unit = self.units.get(server_id)
            requested_at = self._activation_requests.pop(server_id, None)
            start_error: Optional[str] = None
            if unit is None:
                start_error = await self._try_start(server, activation_started_at=requested_at)
            elif (
                requested_at is not None
                or unit.config_hash != server.config_hash
                or unit.state == "unhealthy"
            ):
                await self._stop(server_id)
                start_error = await self._try_start(
                    server, activation_started_at=requested_at or utcnow()
                )

            unit = self.units.get(server_id)
            if unit is not None:
                # Converge the routing key from fresh desired state. slug is
                # excluded from config_hash (a rename must not bounce the bridge),
                # so the branches above never re-derive the unit on a rename. The
                # in-place ``rename_slug`` fast-path can also miss a rename that
                # races this loop (the unit didn't exist yet when it ran, then got
                # started here from a pre-rename snapshot). Re-reading the slug each
                # pass guarantees ``endpoint(<new>)`` resolves within one interval.
                unit.slug = server.slug
                self._write_runtime(
                    server_id,
                    state=unit.state, pid=unit.pid, port=unit.port,
                    last_error=unit.last_error, tools=unit.tools,
                    restart_count=getattr(unit, "restart_count", 0),
                    last_health=getattr(unit, "last_health", None),
                )
            elif start_error is not None:
                # _start raised before creating a unit (port exhaustion / max_running). On the
                # restart path _stop() doesn't write runtime, so the row still carries the prior
                # RUNNING pid/port — clear them here (matching the other failure writes) so a
                # failed server never advertises a pid/port it no longer owns.
                self._write_runtime(
                    server_id, state="failed", pid=None, port=None,
                    last_error=start_error, restart_count=0, last_health=None, tools=[],
                )

    def _write_runtime(self, server_id: str, **fields) -> None:
        """Persist one runtime row in its own short-lived session/transaction, so a slow
        reconcile never holds the SQLite write lock across an ``await`` on docker I/O."""
        with Session(get_engine()) as session:
            repo.upsert_runtime(session, server_id, **fields)

    async def _try_start(self, server, *, activation_started_at=None) -> Optional[str]:
        """Start a unit; return a truncated error string on failure (the caller persists it),
        or ``None`` on success. Kept free of any DB session so the slow spawn stays off the
        reconcile write lock."""
        try:
            await self._start(server, activation_started_at=activation_started_at)
            return None
        except Exception as exc:  # port exhaustion, max_running, spawn failure
            return str(exc)[:300]

    # --- loop ------------------------------------------------------------ #

    async def boot_reset(self) -> None:
        """Observed runtime from a previous process is stale on startup. Reset it
        to stopped so the API reflects reality; reconcile brings servers back."""
        with Session(get_engine()) as session:
            repo.reset_all_runtime(session)
            # The set of docker server ids THIS instance owns. Scoping the sweep to these
            # is what keeps two mcpelevator instances sharing one host daemon from reaping
            # each other's containers. Runs regardless of the docker_runner setting, so a
            # container left running when the runner was turned off is still cleaned up.
            docker_ids = {s.id for s in repo.list_servers(session) if s.runner == "docker"}
        if docker_ids:
            await self._reap_docker_orphans(docker_ids)

    async def _reap_docker_orphans(self, known_ids: set[str]) -> None:
        """Remove containers a prior control-plane process left behind, for servers in
        ``known_ids`` only.

        Graceful stop and the per-unit ``stop()`` reap cover the normal paths; this
        backstops a hard crash, where a container keeps running with no unit to stop it.
        We list our own labelled containers with their server-id label value and remove
        only those whose id is in ``known_ids`` — so a sibling instance sharing the daemon
        (whose server ids live in a different DB) is never touched. Runs on the event loop at
        startup — uses async subprocess (never a blocking ``subprocess.run``) so a slow/wedged
        docker daemon can't stall the whole app's boot — and is silent on any failure (docker
        missing, no daemon reachable, etc.)."""
        out = await _run_docker_capture(
            [DOCKER_BIN, "ps", "-a", "--filter", f"label={LABEL_KEY}", "--format", _PS_FORMAT],
            timeout=8,
        )
        if out is None:
            return  # daemon unreachable / CLI error / timeout — nothing to reap
        ids = []
        for line in out.splitlines():
            parts = line.split()
            # "<container-id> <server-id>"; keep only containers this instance owns.
            if len(parts) == 2 and parts[1] in known_ids:
                ids.append(parts[0])
        if not ids:
            return
        await _run_docker_capture([DOCKER_BIN, "rm", "-f", *ids], timeout=20)

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.reconcile_once()
            except Exception as exc:  # never let the loop die
                print(f"[mcpelevator] reconcile error: {exc}")
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._nudge.clear()

    def nudge(self) -> None:
        """Ask the reconciler to converge now (call from the event-loop thread)."""
        self._nudge.set()

    async def shutdown(self) -> None:
        self._stopping = True
        self.nudge()
        self._activation_requests.clear()
        async with self._unit_lock:
            for server_id in list(self.units):
                await self._stop(server_id)
