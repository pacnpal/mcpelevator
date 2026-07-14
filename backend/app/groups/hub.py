"""GroupHub — owns one FastMCP instance per registry group and their lifecycles.

For each group in the registry, one FastMCP instance mounts a
``create_proxy(ProxyClient(StreamableHttpTransport))`` per running member,
namespaced by slug (tools surface as ``<slug>_<tool>``, collision-safe because
slugs are unique). An instance is REBUILT AND SWAPPED whenever that group's
running membership or the relevant settings change — fastmcp has no ``unmount``,
and a fresh build is trivially correct (no drift). ``sync()`` runs after every
supervisor reconcile pass, so every group converges within one interval of any
change. A group with no running members serves an EMPTY bundle (initialize
works, ``tools/list`` is ``[]``) — deterministic, documented behavior: existence
is decided by the registry alone, membership by what's running.

A mounted-but-dead upstream is not fatal: fastmcp skips a provider that errors
during ``list_tools`` (logging a warning), and per-request proxy sessions mean
only that slug's tools fail — the next reconcile unmounts it anyway.

One security rule sits on top of membership: a member whose EFFECTIVE auth is
stricter than the group's is never mounted — when the default provider is
``none``, a bearer-protected server's tools must not be reachable auth-free
through any ``/g`` endpoint. (When the default is ``bearer``, the group itself
requires a matching token — see ``group_server``.)

The Streamable-HTTP apps run stateless (a fresh transport per request), so
swapping instances never strands client sessions. Starlette does NOT run a
mounted sub-app's lifespan, and anyio lifespans must exit in the task that
entered them — so each instance's lifespan runs inside its own ``_AppRunner``
task, which makes teardown safe from any caller task (reconciler hook, settings
resync, app shutdown).
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient
from sqlmodel import Session
from starlette.types import ASGIApp

# Reuse the bridge's tolerant roots handler: an upstream may ask its client (here,
# the group bundle) to list filesystem roots, and clients without roots support
# must get [] instead of a noisy -32603 (see app.bridge.host._forward_roots).
from app.bridge.host import _forward_roots
from app.db import get_engine, repo
from app.db.models import Server
from app.groups import registry
from app.registry import settings as runtime_settings


def group_server(name: str) -> Server:
    """Pseudo-server for the auth chokepoint (never persisted). ``enforce()`` needs
    a Server: ``inherit`` resolves to the default auth provider (SSOT), and the
    bearer scope check passes tokens scoped ``group:<name>`` (the pseudo id) or
    ``all``, while 403ing per-server and other groups' tokens. Real server ids are
    random hex, so no real scope can collide with the ``group:`` prefix."""
    return Server(
        id=f"group:{name}",
        slug=name,
        name=f"Group {name}",
        auth_provider="inherit",
        args=[],
        env={},
    )


def _default_transport(url: str) -> ClientTransport:
    return StreamableHttpTransport(url=url)


class _AppRunner:
    """Owns one FastMCP http_app lifespan inside a dedicated task.

    Starlette/anyio lifespans must exit in the task that entered them (cancel scopes
    are task-affine), but the hub is driven from several tasks: the reconciler's
    ``on_converged``, a settings/registry resync, and the FastAPI lifespan's
    shutdown. Running the enter/wait/exit sequence in its own task makes teardown
    safe from any caller — ``close()`` just signals the runner and awaits it.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        started: asyncio.Future = asyncio.get_running_loop().create_future()

        async def run() -> None:
            try:
                async with self.app.router.lifespan_context(self.app):
                    started.set_result(None)
                    await self._stop.wait()
            except BaseException as exc:
                if not started.done():
                    started.set_exception(exc)  # startup failure -> surface to start()
                else:
                    raise  # shutdown failure -> surfaced (and logged) by close()

        self._task = asyncio.create_task(run())
        await started

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:
                print(f"[mcpelevator] group lifespan shutdown error: {exc}", flush=True)


class _Instance:
    """One group's live (app, runner, topology-key) triple."""

    __slots__ = ("app", "runner", "key")

    def __init__(self, app: ASGIApp, runner: _AppRunner, key: frozenset) -> None:
        self.app = app
        self.runner = runner
        self.key = key


class GroupHub:
    """Builds and hot-swaps one FastMCP app per registry group from live supervisor
    state."""

    def __init__(
        self, transport_factory: Callable[[str], ClientTransport] = _default_transport
    ) -> None:
        # injectable for tests (in-memory FastMCPTransport instead of loopback HTTP),
        # mirroring how tests patch app.bridge.host._build_transport
        self._transport_factory = transport_factory
        self._instances: dict[str, _Instance] = {}
        self._lock = asyncio.Lock()

    def app_for(self, name: str) -> Optional[ASGIApp]:
        """The group's current Streamable-HTTP ASGI app, or ``None`` (not yet built —
        the dispatcher 503s, a transient during startup/swap)."""
        instance = self._instances.get(name)
        return instance.app if instance is not None else None

    async def sync(self, supervisor) -> None:
        """Converge every group's mounted set to (registry x running units). Called
        after every reconcile pass; serialized so overlapping syncs can't race a
        swap."""
        async with self._lock:
            with Session(get_engine()) as session:
                groups = {
                    name: registry.resolve(session, name)
                    for name in registry.read(session)
                }
                default = runtime_settings.default_auth_provider(session)
                # snapshot to plain values while the session is open — the rows
                # detach on exit, and detached-attribute access must never be
                # load-bearing here
                servers = {
                    s.id: (s.mcp_http, s.auth_provider) for s in repo.list_servers(session)
                }

            running = supervisor.running_endpoints()

            for name in list(self._instances):
                if name not in groups:  # group removed from the registry
                    await self._teardown(name)

            for name, member_ids in groups.items():
                entries: list[tuple[str, str, int]] = []
                members = set(member_ids or [])
                for server_id, slug, host, port in running:
                    if server_id not in members:
                        continue
                    row = servers.get(server_id)
                    if row is None:
                        continue
                    mcp_http, effective = row
                    if not mcp_http:
                        continue
                    if effective == "inherit":
                        effective = default
                    # Anti-downgrade: never mount a member whose own auth is stricter
                    # than the group's, or /g/<name> would bypass its bearer protection.
                    if default == "none" and effective != "none":
                        continue
                    entries.append((slug, host, port))

                key = frozenset(entries)
                current = self._instances.get(name)
                if current is not None and current.key == key:
                    continue  # topology unchanged — keep the live instance
                await self._swap(name, entries, key)

    def _make_proxy(self, slug: str, url: str) -> FastMCP:
        transport = self._transport_factory(url)
        client = ProxyClient(transport, roots=_forward_roots)
        proxy = create_proxy(client, name=slug)
        # Both ProxyClient.__init__ and create_proxy turn ON forwarding of the
        # caller's Authorization header for HTTP transports (caller-credential
        # propagation), so this override must come LAST. On this INTERNAL hop that
        # forwarding would send the group's bearer token — which authorizes the whole
        # bundle — to every bridge, and a remote-runner bridge would forward it once
        # more to its upstream. Never propagate it; bridges are unauthenticated
        # loopback and the group's auth was already enforced at the dispatcher.
        if hasattr(transport, "forward_incoming_headers"):
            transport.forward_incoming_headers = False
        return proxy

    async def _swap(self, name: str, entries: list[tuple[str, str, int]], key: frozenset) -> None:
        bundle = FastMCP(f"mcpelevator-{name}")
        for slug, host, port in sorted(entries):
            bundle.mount(self._make_proxy(slug, f"http://{host}:{port}/mcp"), namespace=slug)
        # stateless: a fresh upstream transport per request, so swapping instances never
        # strands a session. Host/Origin protection stays OFF here — enforce() in the
        # dispatcher is the single guard, identical to every other /s and /g route.
        app = bundle.http_app(path="/mcp", stateless_http=True, host_origin_protection=False)
        # The old instance is torn down before the new one starts (the moment of
        # emptiness just 503s at the dispatcher; swaps only happen on topology
        # changes). Each lifespan runs inside its own _AppRunner task.
        await self._teardown(name)
        runner = _AppRunner(app)
        await runner.start()
        self._instances[name] = _Instance(app, runner, key)

    async def _teardown(self, name: str) -> None:
        old = self._instances.pop(name, None)
        if old is not None:
            await old.runner.close()

    async def close(self) -> None:
        """Shutdown: stop every session manager cleanly (called from the app lifespan)."""
        async with self._lock:
            for name in list(self._instances):
                await self._teardown(name)
