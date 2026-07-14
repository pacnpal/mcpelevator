"""AggregateHub — owns the unified endpoint's FastMCP instance and its lifecycle.

One FastMCP instance mounts a ``create_proxy(ProxyClient(StreamableHttpTransport))``
per included running server, namespaced by slug (tools surface as ``<slug>_<tool>``,
collision-safe because slugs are unique). The instance is REBUILT AND SWAPPED whenever
the running topology or the relevant settings change — fastmcp has no ``unmount``, and
a fresh build is trivially correct (no drift). ``sync()`` runs after every supervisor
reconcile pass, so the mounted set converges within one interval of any change.

A mounted-but-dead upstream is not fatal: fastmcp skips a provider that errors during
``list_tools`` (logging a warning), and per-request proxy sessions mean only that
slug's tools fail — and the next reconcile unmounts it anyway.

Membership is user-controlled (the ``unified_servers`` setting): ``"all"`` means every
running server; a list of server ids restricts to that subset. One security rule on
top: a server whose EFFECTIVE auth is stricter than the aggregate's is never mounted —
when the default provider is ``none``, a bearer-protected server's tools must not be
reachable auth-free through ``/s/all``. (When the default is ``bearer``, the aggregate
itself requires an ``all``-scoped token, which already authorizes every server.)

The Streamable-HTTP app runs stateless (a fresh transport per request), so swapping
instances never strands client sessions. Starlette does NOT run a mounted sub-app's
lifespan, so the hub enters/exits the FastMCP app's lifespan itself on an
``AsyncExitStack`` — that is what starts the session manager for exactly as long as
each instance is live.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Callable, Optional

from fastmcp import FastMCP
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient
from sqlmodel import Session
from starlette.types import ASGIApp

# Reuse the bridge's tolerant roots handler: an upstream may ask its client (here,
# the aggregate) to list filesystem roots, and clients without roots support must
# get [] instead of a noisy -32603 (see app.bridge.host._forward_roots).
from app.bridge.host import _forward_roots
from app.db import get_engine, repo
from app.db.models import Server
from app.registry import settings as runtime_settings

AGGREGATE_SLUG = "all"

# Pseudo-server for the auth chokepoint (never persisted). ``enforce()`` needs a
# Server: ``inherit`` resolves to the default auth provider (SSOT), and the bearer
# scope check passes "all"-scoped tokens while 403ing per-server tokens (no real
# token scope can equal this synthetic id — real ids are random).
AGGREGATE_SERVER = Server(
    id="aggregate",
    slug=AGGREGATE_SLUG,
    name="All servers",
    auth_provider="inherit",
    args=[],
    env={},
)


def _default_transport(url: str) -> ClientTransport:
    return StreamableHttpTransport(url=url)


class AggregateHub:
    """Builds and hot-swaps the aggregate FastMCP app from live supervisor state."""

    def __init__(
        self, transport_factory: Callable[[str], ClientTransport] = _default_transport
    ) -> None:
        # injectable for tests (in-memory FastMCPTransport instead of loopback HTTP),
        # mirroring how tests patch app.bridge.host._build_transport
        self._transport_factory = transport_factory
        self._app: Optional[ASGIApp] = None
        self._stack: Optional[AsyncExitStack] = None
        self._key: Optional[frozenset] = None
        self._lock = asyncio.Lock()

    @property
    def app(self) -> Optional[ASGIApp]:
        """The current Streamable-HTTP ASGI app, or ``None`` (disabled / nothing mounted)."""
        return self._app

    async def sync(self, supervisor) -> None:
        """Converge the mounted set to (settings x running units). Called after every
        reconcile pass; serialized so overlapping syncs can't race a swap."""
        async with self._lock:
            with Session(get_engine()) as session:
                if not runtime_settings.unified_endpoint(session):
                    await self._teardown()
                    return
                selection = runtime_settings.unified_servers(session)
                default = runtime_settings.default_auth_provider(session)
                # snapshot to plain values while the session is open — the rows
                # detach on exit, and detached-attribute access must never be
                # load-bearing here
                servers = {
                    s.id: (s.mcp_http, s.auth_provider) for s in repo.list_servers(session)
                }

            entries: list[tuple[str, str, int]] = []
            for server_id, slug, host, port in supervisor.running_endpoints():
                row = servers.get(server_id)
                if row is None:
                    continue
                mcp_http, effective = row
                if not mcp_http:
                    continue
                if selection != "all" and server_id not in selection:
                    continue
                if effective == "inherit":
                    effective = default
                # Anti-downgrade: never mount a server whose own auth is stricter than
                # the aggregate's, or /s/all would bypass its bearer protection.
                if default == "none" and effective != "none":
                    continue
                entries.append((slug, host, port))

            key = frozenset(entries)
            if key == self._key and (self._app is not None or not entries):
                return  # topology unchanged — keep the live instance (and its sessions)

            if not entries:
                await self._teardown()
                self._key = key  # remember emptiness so quiet reconciles stay no-ops
                return
            await self._swap(entries, key)

    async def _swap(self, entries: list[tuple[str, str, int]], key: frozenset) -> None:
        agg = FastMCP("mcpelevator-all")
        for slug, host, port in sorted(entries):
            transport = self._transport_factory(f"http://{host}:{port}/mcp")
            client = ProxyClient(transport, roots=_forward_roots)
            agg.mount(create_proxy(client, name=slug), namespace=slug)
        # stateless: a fresh upstream transport per request, so swapping instances never
        # strands a session. Host/Origin protection stays OFF here — enforce() in the
        # dispatcher is the single guard, identical to every other /s route.
        app = agg.http_app(path="/mcp", stateless_http=True, host_origin_protection=False)
        # anyio cancel scopes are strictly LIFO within a task: the OLD lifespan must
        # exit before the new one is entered, or the second swap raises "attempted to
        # exit a cancel scope that isn't the current task's". The instant of emptiness
        # is fine — a request in the gap gets the dispatcher's 503, and swaps only
        # happen on topology changes.
        await self._teardown()
        stack = AsyncExitStack()
        await stack.enter_async_context(app.router.lifespan_context(app))
        self._app, self._stack, self._key = app, stack, key

    async def _teardown(self) -> None:
        old, self._app, self._stack, self._key = self._stack, None, None, None
        if old is not None:
            await old.aclose()

    async def close(self) -> None:
        """Shutdown: stop the session manager cleanly (called from the app lifespan)."""
        async with self._lock:
            await self._teardown()
