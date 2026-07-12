"""ServerUnit — the live, in-memory representation of one elevated server.

Owns the bridge-host subprocess and drives its state machine:

    stopped -> starting -> running
                  |  \\-> failed (spawn or readiness timeout)
                  \\----> (process exit handled by the log pump)

Readiness is a real end-to-end probe: connect a FastMCP client to the bridge's
``/mcp`` over HTTP and ``list_tools()``. Success means a client genuinely can use
the server (and we cache the tool list for the UI). The probe also warms the
npx/uvx package cache, so the first real client call is fast.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Optional

from fastmcp import Client

from app.config import get_settings
from app.db.models import Server
from app.runners import build_spec
from app.runners.docker import DOCKER_BIN, server_label
from app.supervisor.logbuffer import LogBuffer


def tool_summary(tool) -> dict:
    """One cached-tool entry for the UI, from a probed ``mcp.types.Tool``.

    ``has_output_schema`` mirrors the hint MCP clients and app-review tools
    surface: a tool without an ``outputSchema`` gets a "recommended: add one so
    models can better understand this tool's results" nudge. The schema is
    authored upstream and proxied through unchanged, so this flag is diagnostic —
    it tells the operator which upstream tools lack one, not something mcpelevator
    can fix.
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "has_output_schema": tool.outputSchema is not None,
    }


class ServerUnit:
    def __init__(self, server: Server):
        # snapshot primitives — never hold a live ORM object
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
        self.state = "stopped"
        self.last_error: Optional[str] = None
        self.tools: list[dict] = []
        self.logs = LogBuffer()
        self._tasks: set[asyncio.Task] = set()

    # --- lifecycle ------------------------------------------------------- #

    async def start(self, port: int) -> None:
        self.port = port
        self.state = "starting"
        self.last_error = None
        settings = get_settings()

        payload = {
            "command": self.spec.command,
            "args": self.spec.args,
            "env": self.spec.env,
            "cwd": self.spec.cwd,
            "transport": self.spec.transport,
            "minimal_env": self.spec.minimal_env,
            "name": self.name,
            **self.exposure,
        }
        env = {
            **os.environ,
            "MCPE_BRIDGE_SPEC": json.dumps(payload),
            "MCPE_BRIDGE_HOST": self.host,
            "MCPE_BRIDGE_PORT": str(port),
            "PYTHONPATH": str(settings.backend_root),
        }
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "app.bridge.host",
            cwd=str(settings.backend_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group -> clean signal of the whole tree
        )
        self.logs.append(f"[mcpelevator] starting bridge pid={self.proc.pid} port={port}")
        self._spawn(self._pump_logs())
        self._spawn(self._await_ready())

    async def stop(self) -> None:
        self.state = "stopping"
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.runner == "docker":
            await self._reap_container()
        self.state = "stopped"
        self.port = None

    async def _reap_container(self) -> None:
        """Best-effort ``docker rm -f`` for every container this server launched.

        The graceful path already cleans up (SIGTERM to the group → docker CLI forwards it
        → container stops → ``--rm`` removes). This backstops the SIGKILL path, where the
        CLI was killed before it could tell the daemon to stop, leaving the container
        running. Reaping is by LABEL (not name), so it also covers the case where FastMCP
        opened more than one upstream container for this server. Fire-and-forget with its
        OWN short timeout so a slow/wedged daemon can't stall stop()/reconcile; every
        failure mode is ignored (the boot label-sweep is the final backstop)."""
        label = server_label(self.id)
        try:
            listed = await asyncio.create_subprocess_exec(
                DOCKER_BIN, "ps", "-aq", "--filter", f"label={label}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            return  # docker CLI not present — nothing we can do here
        try:
            out, _ = await asyncio.wait_for(listed.communicate(), timeout=8)
        except asyncio.TimeoutError:
            try:
                listed.kill()
            except ProcessLookupError:
                pass
            return
        ids = out.decode(errors="replace").split()
        if not ids:
            return
        try:
            rm = await asyncio.create_subprocess_exec(
                DOCKER_BIN, "rm", "-f", *ids,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm.wait(), timeout=8)
        except (FileNotFoundError, OSError):
            return
        except asyncio.TimeoutError:
            try:
                rm.kill()
            except ProcessLookupError:
                pass

    # --- background tasks ------------------------------------------------ #

    async def _pump_logs(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        async for raw in self.proc.stdout:
            self.logs.append(raw.decode(errors="replace").rstrip("\n"))
        rc = await self.proc.wait()
        self.logs.append(f"[mcpelevator] bridge exited rc={rc}")
        # Only flag failure if we weren't deliberately stopping.
        if self.state == "running":
            self.state = "unhealthy"
            self.last_error = f"bridge exited rc={rc}"
        elif self.state == "starting":
            self.state = "failed"
            self.last_error = f"bridge exited rc={rc}"

    async def _await_ready(self) -> None:
        settings = get_settings()
        url = f"http://{self.host}:{self.port}/mcp"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + settings.start_timeout_s
        while loop.time() < deadline:
            if self.proc is None or self.proc.returncode is not None:
                return  # exited; _pump_logs owns the state transition
            try:
                async with Client(url, timeout=settings.start_timeout_s) as client:
                    tools = await client.list_tools()
                self.tools = [tool_summary(t) for t in tools]
                if self.state == "starting":
                    self.state = "running"
                    self.last_error = None
                    self.logs.append(f"[mcpelevator] ready — {len(self.tools)} tool(s)")
                return
            except Exception as exc:  # connection refused while warming, etc.
                self.last_error = str(exc)[:300]
                await asyncio.sleep(2)
        if self.state == "starting":
            self.state = "failed"
            self.last_error = self.last_error or "readiness timeout"
            # A docker unit that never became ready (bad env/args, dind not up) may have left
            # a labelled container running while the bridge process stays alive — the M1
            # reconciler won't restart a failed unit (config_hash unchanged), so nothing else
            # would reap it. Best-effort reap here (idempotent with stop()).
            if self.runner == "docker":
                await self._reap_container()

    # --- helpers --------------------------------------------------------- #

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc is not None else None

    def is_dead(self) -> bool:
        return self.proc is None or self.proc.returncode is not None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
