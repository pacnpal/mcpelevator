"""GroupHub — owns one FastMCP instance per registry group and their lifecycles.

For each group in the registry, one FastMCP instance mounts a
``create_proxy(ProxyClient(StreamableHttpTransport))`` per running member,
namespaced by slug (tools surface as ``<slug>_<tool>``, collision-safe because
slugs are unique). The members sit in one ``AggregateProvider`` per instance, and a
change to the group's running membership or the relevant settings adds and removes
just the members that changed, IN PLACE: the instance, and every client session on
it, carries on, and every session sees the new membership at once (a member the
anti-downgrade rule now excludes is gone for all of them). ``sync()`` runs after
every supervisor reconcile pass, so every group converges within one interval of
any change, and each change is announced to the group's clients as a
``list_changed`` (handshake-era sessions on their standalone stream, 2026-07-28 callers
on ``subscriptions/listen``). An instance is rebuilt only after a teardown (an auth-affecting write,
or a failed sync). A group with no running members serves an EMPTY bundle (initialize
works, ``tools/list`` is ``[]``) — deterministic, documented behavior: existence
is decided by the registry alone, membership by what's running.

A mounted-but-dead upstream is not fatal: fastmcp skips a provider that errors
during ``list_tools`` (logging a warning), and per-request proxy sessions mean
only that slug's tools fail — the next reconcile unmounts it anyway.

One security rule sits on top of membership: a protected member is mounted only
when the group uses the same provider. ``bearer`` and ``oauth`` are separate
credential systems, so neither can stand in for the other. An explicitly open
member is safe behind any group provider.

The Streamable-HTTP apps are stateful, so a handshake-era client gets a session and
with it the back-channel a member's roots/sampling/elicitation requests are pushed
on (a stateless request has nowhere for the client's reply to land). Since an
instance outlives membership changes, those sessions survive them; only a teardown
ends them, and the client then initializes a new one. Starlette does NOT run a
mounted sub-app's lifespan, and anyio lifespans must exit in the task that
entered them — so each instance's lifespan runs inside its own ``_AppRunner``
task, which makes teardown safe from any caller task (reconciler hook, settings
resync, app shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from contextlib import ExitStack, asynccontextmanager
from typing import Callable, Optional

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from mcp.server.request_state import RequestStateSecurity
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    PromptsListChanged,
    ResourcesListChanged,
    ToolsListChanged,
)
from mcp_types import SubscriptionsListenRequestParams
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.aggregate import AggregateProvider
from fastmcp.server.transforms import Transform
from sqlmodel import Session
from starlette.types import ASGIApp

# Reuse the bridge's tolerant roots handler: an upstream may ask its client (here,
# the group bundle) to list filesystem roots, and clients without roots support
# must get [] instead of a noisy -32603 (see app.bridge.host._forward_roots).
from app.bridge.host import (
    _forward_roots,
    _is_modern,
    _ModernTurns,
    _TurnAwareProxyClient,
    represents,
)
from app.db import get_engine, repo
from app.db.models import BUILTIN_RUNNER, Server
from app.groups import registry
from app.registry import settings as runtime_settings

logger = logging.getLogger(__name__)

_LIST_CHANGED = (
    "notifications/tools/list_changed",
    "notifications/resources/list_changed",
    "notifications/prompts/list_changed",
)
_ANNOUNCE_S = 5.0  # how long a membership change waits on its clients' list_changed


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
            except Exception:
                logger.exception("group lifespan shutdown error")


# How long a replaced group instance may keep serving the requests it already
# accepted before it is closed anyway.
_DRAIN_S = 300.0


class _Counting:
    """A group's ASGI app, counting requests in flight. A teardown routes new
    requests away at once and closes the instance only when this reaches zero, so a
    call that changes its own group's auth (the built-in server through /g, say)
    isn't cut off by the teardown it caused."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active = 0
        self.idle = asyncio.Event()
        self.idle.set()

    def hold(self) -> None:
        self.active += 1
        self.idle.clear()

    def release(self) -> None:
        self.active -= 1
        if not self.active:
            self.idle.set()

    async def __call__(self, scope, receive, send) -> None:
        self.hold()
        try:
            await self.app(scope, receive, send)
        finally:
            self.release()


class _ParkedTurns(dict):
    """A group's parked turns. ``closed`` once a teardown detaches the table: a request
    still running on the retired instance then can't park in it, since no retry would
    ever find it there (``_Turn.leg`` refuses its input request instead)."""

    closed = False


class _Sessions(Middleware):
    """Remembers the group's handshake-era connections, so a membership change can tell
    each one its lists changed (see :meth:`GroupHub._announce`).

    The SDK's ``ServerSession`` is a facade built per request; the ``Connection`` behind
    it lives as long as the client's session does, and its ``outbound`` is the
    standalone stream. No public accessor reaches it from a request yet, hence
    ``_connection``."""

    def __init__(self) -> None:
        self.connections: weakref.WeakSet = weakref.WeakSet()

    async def on_request(self, context, call_next):
        ctx = context.fastmcp_context
        if ctx is not None and ctx.request_context is not None and not _is_modern(ctx):
            connection = getattr(ctx.session, "_connection", None)
            if connection is not None:
                self.connections.add(connection)
        return await call_next(context)


class _Instance:
    """One group's live app, runner, topology key and mounted members."""

    __slots__ = (
        "app", "runner", "key", "members", "bundle", "slot", "runtime", "sessions", "bus",
        "listen",
    )

    def __init__(
        self,
        app: ASGIApp,
        runner: _AppRunner,
        key: frozenset,
        members: dict[str, str] | None = None,
        bundle: FastMCP | None = None,
    ) -> None:
        self.app = app
        self.runner = runner
        self.key = key
        # The members live in one AggregateProvider at ``bundle.providers[slot]``. A
        # membership change builds a new one and swaps it in whole (see _install), so a
        # request in flight keeps the snapshot it started with; ``runtime`` holds the
        # live snapshot's extension-runtime contexts, closed when it's replaced.
        self.bundle = bundle
        self.slot: int | None = None
        self.runtime: ExitStack | None = None
        # Where a membership change is announced: the handshake-era sessions, and the
        # subscriptions/listen streams of 2026-07-28 callers (set by GroupHub._build).
        self.sessions: _Sessions | None = None
        self.bus: InMemorySubscriptionBus | None = None
        self.listen: ListenHandler | None = None
        # slug -> server id for exactly the members mounted in this instance.
        # Captured at build time from the supervisor's running set, so a caller
        # that needs the id behind a namespace (usage attribution) reads it from
        # here instead of querying every registered server per request.
        self.members = members or {}


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
        self._retiring: set[_Instance] = set()  # replaced, finishing their requests
        self._lock = asyncio.Lock()
        # A modern caller's interactive request spans several round trips (bridge.host
        # _Turn), and its group's instance can be torn down and rebuilt between two of
        # them. Every bundle seals its request_state under ONE key (each still binds its
        # own group's name as the audience), so the retry is still recognized and told
        # its request has ended, instead of failing as a forged token.
        self._request_state = RequestStateSecurity.ephemeral()
        self._parked: dict[str, dict] = {}
        self._supervisor = None  # set by sync(); holds a parked turn's members awake

    def app_for(self, name: str) -> Optional[ASGIApp]:
        """The group's current Streamable-HTTP ASGI app, or ``None`` (not yet built —
        the dispatcher 503s, a transient during startup/swap)."""
        instance = self._instances.get(name)
        return instance.app if instance is not None else None

    def mounted_members(self, name: str) -> dict[str, str]:
        """``{slug: server_id}`` for the members this group is CURRENTLY serving.

        Registry membership is not the same set: a member that isn't running, has
        `mcp_http` off, or is excluded by the anti-downgrade rule is configured but
        not mounted, so the bundle has no provider for its namespace. Callers that
        reason about what a group can actually serve — usage attribution, say —
        must ask this, not the registry, or a namespaced call to a tool the bundle
        rejects would be credited to a member that never saw it.

        Carrying the id, not just the slug, is what keeps attribution off the
        database: resolving a namespace to a server otherwise meant loading every
        registered server on each group call, synchronously, on the event loop."""
        instance = self._instances.get(name)
        return dict(instance.members) if instance is not None else {}

    def mounted_slugs(self, name: str) -> set[str]:
        """Just the slugs of :meth:`mounted_members`."""
        return set(self.mounted_members(name))

    async def sync(self, supervisor) -> None:
        """Converge every group's mounted set to (registry x running units). Called
        after every reconcile pass; serialized so overlapping syncs can't race a
        swap."""
        self._supervisor = supervisor
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
                aliases = {s.id: _member_aliases(s) for s in repo.list_servers(session)}
                builtin = {s.id for s in repo.list_servers(session) if s.runner == BUILTIN_RUNNER}

            running = supervisor.running_endpoints()

            for name in list(self._instances):
                if name not in groups:  # group removed from the registry
                    await self._teardown(name)

            for name, member_ids in groups.items():
                entries: list[tuple[str, str, str, int]] = []
                mounted: dict[str, str] = {}
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
                    # Anti-downgrade: a protected member must use the group's provider.
                    # Bearer and OAuth are incomparable credential systems; authenticating
                    # with one must not bypass a member protected by the other. An open
                    # member is safe behind any group provider.
                    if effective != "none" and effective != default:
                        continue
                    # The built-in server reads the caller's control credential, which
                    # an OAuth group never carries (its token is the provider's JWT),
                    # so there it would only list tools that refuse every call.
                    if server_id in builtin and default == "oauth":
                        continue
                    # The entry carries the server ID, so the topology key below
                    # does too. Ports are runtime, not identity, and the allocator
                    # hands back the LOWEST free one — so deleting a member and
                    # creating a replacement with the same slug can land on the
                    # same port and leave (slug, host, port) identical. Keyed on
                    # that alone the swap is skipped, and the instance keeps
                    # serving with the DELETED server's id in `members`,
                    # attributing every later group call to a row `bump_usage`
                    # then drops on the floor.
                    entries.append((server_id, slug, host, port))
                    mounted[slug] = server_id

                # A member's aliases shape what its proxy answers to, so a change to them
                # is a topology change too (absent when there are none, keeping the key
                # the plain entry set).
                key = frozenset(entries) | frozenset(
                    ("aliases", server_id, aliases[server_id])
                    for server_id, *_ in entries
                    if aliases.get(server_id)
                )
                current = self._instances.get(name)
                if current is not None and current.key == key:
                    continue  # topology unchanged — keep the live instance
                try:
                    await self._swap(name, entries, key, mounted, builtin, aliases)
                except Exception:
                    # Isolate per group: one group's build/start failure must not abort the
                    # whole pass and starve every group after it in iteration order (the
                    # reconciler only wraps the whole on_converged call). Fail CLOSED for the
                    # bad group: tear down any surviving instance so app_for(name) is None
                    # and the dispatcher 503s it, rather than serving a stale bundle (e.g.
                    # one whose auth was just tightened) — _swap can fail either before or
                    # after its own teardown, so we force it here. Other groups still
                    # converge this pass; the reconciler retries the failed one next pass.
                    logger.exception("group %r sync error", name)
                    await self._teardown(name)

    def _make_proxy(
        self,
        slug: str,
        url: str,
        forward: bool = False,
        server_id: str | None = None,
        aliases: tuple = (),
    ) -> FastMCP:
        # ProxyClient forwards the caller's inbound headers (Authorization included) by
        # default. On this INTERNAL hop that would send the group's bearer token — which
        # authorizes the whole bundle — to every bridge, and a remote-runner bridge would
        # forward it once more to its upstream. Never propagate it; bridges are
        # unauthenticated loopback and the group's auth was already enforced at the
        # dispatcher. The one exception is the built-in mcpelevator server (``forward``):
        # its bridge only talks to this process's /api/mcp, which authorizes every call
        # as the caller and so needs the caller's own credential.
        client = _TurnAwareProxyClient(
            self._transport_factory(url), roots=_forward_roots, forward_incoming_headers=forward
        )
        client.member = server_id
        proxy = create_proxy(client, name=slug)
        if aliases:
            proxy.add_transform(_MemberAliases(aliases))
        return proxy

    async def _swap(
        self,
        name: str,
        entries: list[tuple[str, str, str, int]],
        key: frozenset,
        members: dict[str, str] | None = None,
        forward: set[str] | None = None,
        aliases: dict[str, tuple] | None = None,
    ) -> None:
        """Bring group ``name`` to ``entries``: build its instance if there's none, then
        install the new membership in it, in place."""
        parked = self._parked.setdefault(name, _ParkedTurns())
        # A parked request may only resume if every member it reached is still in the
        # group: a member that left, or is now excluded (the anti-downgrade rule, say),
        # must not answer through it. Ending those now frees their upstream sessions;
        # the resume check in _ModernTurns covers a turn that parks after this.
        mounted_ids = {server_id for server_id, *_ in entries}
        for turn in list(parked.values()):
            if not turn.members <= mounted_ids:
                turn.end()
        instance = self._instances.get(name)
        fresh = instance is None
        if fresh:
            instance = await self._build(name, parked)
            self._instances[name] = instance
        members_provider = AggregateProvider()  # built offline: nothing runs it yet
        for server_id, slug, host, port in sorted(entries):
            proxy = self._make_proxy(
                slug,
                f"http://{host}:{port}/mcp",
                forward=server_id in (forward or ()),
                server_id=server_id,
                aliases=(aliases or {}).get(server_id, ()),
            )
            members_provider.add_provider(proxy, namespace=slug)
        self._install(instance, members_provider)
        instance.key = key
        instance.members = members or {}
        if not fresh:
            await self._announce(instance)

    def _hold(self, members: set[str]) -> Callable[[], None]:
        """Keep ``members`` in flight while a turn that reached them is parked, so the
        idle sweep can't stop one under it; returns the call that lets them go."""
        supervisor = self._supervisor
        if supervisor is None or not members:
            return lambda: None
        for member_id in members:
            supervisor.request_started(member_id)

        def release() -> None:
            for member_id in members:
                supervisor.request_finished(member_id)

        return release

    @staticmethod
    async def _announce(instance: _Instance) -> None:
        """Tell the group's clients its tools, resources and prompts changed: each
        handshake-era session on its standalone stream, and each 2026-07-28 caller on
        its ``subscriptions/listen`` stream. A session that's gone is skipped."""
        for event in (ToolsListChanged(), ResourcesListChanged(), PromptsListChanged()):
            await instance.bus.publish(event)

        async def tell(connection) -> None:
            try:
                for method in _LIST_CHANGED:
                    await connection.outbound.notify(method, None)
            except Exception:
                logger.debug("a group session closed before its list_changed", exc_info=True)

        # All at once, under one deadline: this runs under sync's lock, and a client
        # that stopped reading must not hold every group's convergence up behind it.
        sends = [asyncio.ensure_future(tell(c)) for c in list(instance.sessions.connections)]
        if sends:
            _, stuck = await asyncio.wait(sends, timeout=_ANNOUNCE_S)
            for send in stuck:
                send.cancel()

    @staticmethod
    def _install(instance: _Instance, members_provider: AggregateProvider) -> None:
        """Make ``members_provider`` the instance's membership, atomically.

        One list-item assignment: a request already in flight keeps iterating the
        snapshot it started with (FastMCP's aggregate re-indexes its provider list after
        awaiting, so shrinking a live one could raise), and every request after it sees
        the new one. FastMCP enters each provider's extension runtime into its running
        scopes when a provider is ADDED, with no way to leave it on removal; installing
        by assignment skips that, and the snapshot's runtime is entered here instead, on
        a stack of its own that's closed when the next snapshot replaces it."""
        bundle = instance.bundle
        runtime = ExitStack()
        for scope in bundle._extension_scopes:
            runtime.enter_context(
                members_provider._extension_runtime(scope.available, root=scope.root)
            )
        bundle.providers[instance.slot] = members_provider
        previous, instance.runtime = instance.runtime, runtime
        if previous is not None:
            previous.close()

    async def _build(self, name: str, parked: dict) -> _Instance:
        bundle = FastMCP(f"mcpelevator-{name}", request_state_security=self._request_state)
        sessions = _Sessions()
        bundle.add_middleware(sessions)
        bundle.add_middleware(
            _ModernTurns(
                parked,
                members=lambda: set(self.mounted_members(name).values()),
                hold=self._hold,
            )
        )
        # 2026-07-28 callers have no standing stream; they hear about membership
        # changes by listening. Serving the method also makes the bundle advertise
        # listChanged to them.
        bus = InMemorySubscriptionBus()
        listen = ListenHandler(bus)
        bundle._mcp_server.add_request_handler(
            "subscriptions/listen", SubscriptionsListenRequestParams, listen
        )
        # A placeholder for the membership _install swaps in: empty, so the runtime
        # FastMCP keeps for it at startup holds nothing.
        bundle.add_provider(AggregateProvider())
        # Stateful, for the handshake-era back-channel (see the module docstring).
        # Host/Origin protection stays OFF here — enforce() in the dispatcher is the
        # single guard, identical to every other /s and /g route.
        app = bundle.http_app(path="/mcp", stateless_http=False, host_origin_protection=False)
        # Each lifespan runs inside its own _AppRunner task.
        runner = _AppRunner(app)
        await runner.start()
        instance = _Instance(_Counting(app), runner, frozenset(), bundle=bundle)
        instance.slot = len(bundle.providers) - 1
        instance.sessions, instance.bus, instance.listen = sessions, bus, listen
        return instance

    async def _teardown(self, name: str) -> None:
        """Stop routing to ``name`` now; its instance closes once drained."""
        parked = self._parked.pop(name, _ParkedTurns())
        parked.closed = True
        for turn in list(parked.values()):
            turn.end()  # nothing serves this group now, so nothing may resume these
        old = self._instances.pop(name, None)
        if old is not None:
            self._retire(old)

    def _retire(self, instance: _Instance) -> None:
        # Never awaited by the caller: the caller may be a request running ON this
        # instance (a control-plane call that rebuilt its own group), so waiting
        # for the drain here would deadlock.
        self._retiring.add(instance)
        listen = getattr(instance, "listen", None)
        if listen is not None:
            listen.close()  # end listen streams now; the drain shouldn't wait on them

        async def drain() -> None:
            try:
                await asyncio.wait_for(instance.app.idle.wait(), _DRAIN_S)
            except asyncio.TimeoutError:
                logger.warning("group instance still busy after %ss; closing it", _DRAIN_S)
            finally:
                if instance in self._retiring:
                    self._retiring.discard(instance)
                    await instance.runner.close()

        asyncio.get_running_loop().create_task(drain())

    async def _close_all_unlocked(self) -> None:
        instances = [*self._instances.values(), *self._retiring]
        self._instances.clear()
        self._retiring.clear()
        for instance in instances:
            try:
                await instance.runner.close()
            except Exception:
                # Keep closing the remaining runners. The routing registry is already
                # empty, so losing later instances here would make them unreachable
                # for a subsequent cleanup attempt.
                logger.exception("group runner close failed")

    @asynccontextmanager
    async def auth_transition(self):
        """Keep every group unavailable while an auth-affecting row is written. New
        requests 503 at once; requests already accepted under the old auth finish
        (one of them may be the very call making this change)."""
        async with self._lock:
            for name in list(self._instances):
                await self._teardown(name)
            yield

    async def close(self) -> None:
        """Shutdown: stop every session manager cleanly (called from the app lifespan,
        and after a sync failure, while the process keeps running)."""
        async with self._lock:
            # Nothing serves any group after this, so nothing may resume what's parked:
            # end it now, rather than holding upstream sessions open to the TTL.
            for parked in self._parked.values():
                parked.closed = True
                for turn in list(parked.values()):
                    turn.end()
            self._parked.clear()
            for instance in [*self._instances.values(), *self._retiring]:
                runtime = getattr(instance, "runtime", None)
                if runtime is not None:
                    runtime.close()
                    instance.runtime = None
            # Invalidate the whole routing registry before awaiting any teardown. A
            # runner close can block or fail, but no stale group app may remain
            # reachable while shutdown is in progress (including fail-closed shutdown
            # after a broad sync failure).
            await self._close_all_unlocked()


def _member_aliases(server: Server) -> tuple:
    """A member's tool aliases as ``(alias, upstream name, rename or None)``, sorted."""
    return tuple(sorted(
        (alias, tool, (override or {}).get("name"))
        for tool, override in (server.tool_overrides or {}).items()
        for alias in (override or {}).get("aliases") or ()
    ))


class _MemberAliases(Transform):
    """Answer a member's tool aliases in the group.

    The member's bridge serves its aliases (``bridge.host._ToolTransform``) but never
    lists them, and the group's proxy only finds a tool in the member's tool LIST, so it
    has to know them itself. An alias applies only when the member lists no tool of that
    name, and resolves to the tool as listed: under its rename when the rename applied
    (the listed tool carries its upstream name in ``_meta``), else under its own name."""

    def __init__(self, aliases: tuple) -> None:
        self._aliases = {alias: (tool, renamed) for alias, tool, renamed in aliases}

    async def get_tool(self, name, call_next, *, version=None):
        found = await call_next(name, version=version)
        target = self._aliases.get(name)
        if found is not None or target is None:
            return found
        tool, renamed = target
        # What the member lists under the name must BE the alias's tool: another tool
        # renamed onto a hidden or vanished one's name is not it.
        for candidate in (renamed, tool):
            if not candidate:
                continue
            listed = await call_next(candidate, version=version)
            if listed is not None and represents(listed, tool):
                return listed
        return None
