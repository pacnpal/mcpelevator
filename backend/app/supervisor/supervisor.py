"""Supervisor — the single owner of runtime state.

API handlers never spawn or kill processes. They write desired state (SQLite) and
``nudge()`` this reconciler, which converges actual -> desired (Kubernetes-style).
That indirection is what makes the system idempotent and deterministic: re-running
``reconcile_once`` with unchanged desired state is a no-op, and ``config_hash`` is
the anchor that decides when a restart is actually needed.

M1 scope: start desired, stop undesired, restart on config change, persist observed
state. Health probing + restart budgets land in M2.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Optional

from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine, repo
from app.registry import settings as runtime_settings
from app.runners.docker import DOCKER_BIN, LABEL_KEY
from app.supervisor.ports import PortAllocator
from app.supervisor.unit import ServerUnit

# `docker ps` Go-template that prints "<container-id> <server-id-label>" per line, so the
# boot sweep can keep only containers whose label value is a server in THIS instance's DB.
_PS_FORMAT = '{{.ID}} {{.Label "' + LABEL_KEY + '"}}'


class Supervisor:
    def __init__(self) -> None:
        settings = get_settings()
        self.ports = PortAllocator(settings.port_range_start, settings.port_range_end)
        self.max_running = settings.max_running
        self.interval = settings.health_interval_s
        self.units: dict[str, ServerUnit] = {}
        self._nudge = asyncio.Event()
        self._stopping = False

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

    # --- start/stop a single unit ---------------------------------------- #

    async def _start(self, server) -> None:
        if len(self.units) >= self.max_running:
            raise RuntimeError(f"max_running ({self.max_running}) reached")
        port = self.ports.allocate()
        unit = ServerUnit(server)
        self.units[server.id] = unit
        await unit.start(port)

    async def _stop(self, server_id: str) -> None:
        unit = self.units.pop(server_id, None)
        if unit is not None:
            port = unit.port
            await unit.stop()
            if port is not None:
                self.ports.release(port)

    async def stop(self, server_id: str) -> None:
        """Public stop (e.g. API-driven delete). Steady state is still reconciled."""
        await self._stop(server_id)

    # --- reconcile ------------------------------------------------------- #

    async def reconcile_once(self) -> None:
        with Session(get_engine()) as session:
            docker_on = runtime_settings.docker_runner(session)
            desired: dict[str, object] = {}
            for sv in repo.list_servers(session):
                if not sv.enabled:
                    continue
                # The docker runner is root-equivalent and opt-in: if it's off, an enabled
                # docker server must not run. Stop any live unit and surface why, rather
                # than silently converging it — this also catches the setting being turned
                # off while a docker server is running (within one reconcile interval).
                if sv.runner == "docker" and not docker_on:
                    if sv.id in self.units:
                        await self._stop(sv.id)
                    repo.upsert_runtime(
                        session, sv.id, state="failed", pid=None, port=None,
                        last_error="Docker runner is disabled (enable it in Settings)", tools=[],
                    )
                    continue
                desired[sv.id] = sv

            # stop anything running that is no longer desired
            for server_id in list(self.units):
                if server_id not in desired:
                    await self._stop(server_id)
                    repo.upsert_runtime(
                        session, server_id, state="stopped", pid=None, port=None,
                        last_error=None, tools=[],
                    )

            # start / restart desired
            for server_id, server in desired.items():
                unit = self.units.get(server_id)
                if unit is None:
                    await self._try_start(session, server)
                elif unit.config_hash != server.config_hash:
                    await self._stop(server_id)
                    await self._try_start(session, server)

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
                    repo.upsert_runtime(
                        session, server_id,
                        state=unit.state, pid=unit.pid, port=unit.port,
                        last_error=unit.last_error, tools=unit.tools,
                    )

    async def _try_start(self, session: Session, server) -> None:
        try:
            await self._start(server)
        except Exception as exc:  # port exhaustion, max_running, spawn failure
            repo.upsert_runtime(session, server.id, state="failed", last_error=str(exc)[:300])

    # --- loop ------------------------------------------------------------ #

    def boot_reset(self) -> None:
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
            self._reap_docker_orphans(docker_ids)

    def _reap_docker_orphans(self, known_ids: set[str]) -> None:
        """Remove containers a prior control-plane process left behind, for servers in
        ``known_ids`` only.

        Graceful stop and the per-unit ``stop()`` reap cover the normal paths; this
        backstops a hard crash, where a container keeps running with no unit to stop it.
        We list our own labelled containers with their server-id label value and remove
        only those whose id is in ``known_ids`` — so a sibling instance sharing the daemon
        (whose server ids live in a different DB) is never touched. Runs synchronously at
        boot (before the event loop starts) and is silent on any failure (docker missing,
        no daemon reachable, etc.)."""
        try:
            listed = subprocess.run(
                [DOCKER_BIN, "ps", "-a", "--filter", f"label={LABEL_KEY}", "--format", _PS_FORMAT],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return
        if listed.returncode != 0:  # daemon unreachable / CLI error — nothing to reap
            return
        ids = []
        for line in listed.stdout.splitlines():
            parts = line.split()
            # "<container-id> <server-id>"; keep only containers this instance owns.
            if len(parts) == 2 and parts[1] in known_ids:
                ids.append(parts[0])
        if not ids:
            return
        try:
            subprocess.run([DOCKER_BIN, "rm", "-f", *ids], capture_output=True, timeout=30)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass

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
        for server_id in list(self.units):
            await self._stop(server_id)
