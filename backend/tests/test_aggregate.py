"""Unified endpoint tests — the aggregate hub, its /s/all dispatcher, and settings.

The hub is exercised fully in-memory: upstream FastMCP servers stand in for the
loopback bridges via the hub's injectable transport factory (the same seam
``test_bridge_host`` patches), and a fake supervisor supplies the running topology.
No subprocess is ever spawned. Route tests reuse the ``test_proxy`` patterns
(TestClient + stubbed supervisor/hub state).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from fastmcp.client.transports import FastMCPTransport
from sqlmodel import Session
from starlette.responses import JSONResponse

from app.aggregate.hub import AggregateHub
from app.db import get_engine, repo
from app.db.models import Server, Token
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token

LOOPBACK = {"host": "127.0.0.1"}

# settings written by these tests, restored to defaults afterwards
_SETTINGS_KEYS = ("unified_endpoint", "unified_servers", "default_auth_provider")


@pytest.fixture(autouse=True, scope="module")
def _init_db():
    """Hub tests hit the shared engine directly (no TestClient lifespan to create
    tables first)."""
    from app.db import init_db

    init_db()


@pytest.fixture
def clean_settings():
    yield
    with Session(get_engine()) as session:
        runtime_settings.write(
            session, {k: runtime_settings.DEFAULTS[k] for k in _SETTINGS_KEYS}
        )


def _write_settings(**changes) -> None:
    with Session(get_engine()) as session:
        runtime_settings.write(session, changes)


def _make_upstream(name: str, tool_name: str) -> FastMCP:
    srv = FastMCP(name)

    @srv.tool(name=tool_name)
    def tool(q: str) -> str:
        return f"{name}:{q}"

    return srv


class _BrokenTransport(FastMCPTransport):
    """Fails on connect, like a bridge whose process just died."""

    def __init__(self):
        super().__init__(FastMCP("dead"))

    def connect_session(self, **kwargs):
        raise ConnectionError("connection refused (dead bridge)")


def _hub_for(upstreams: dict[str, FastMCP | FastMCPTransport]) -> AggregateHub:
    """Hub whose transport factory resolves the synthetic bridge URLs to in-memory
    upstreams, keyed by host (see ``_endpoints``)."""

    def factory(url: str):
        host = url.removeprefix("http://").split(":", 1)[0]
        upstream = upstreams[host]
        return upstream if isinstance(upstream, FastMCPTransport) else FastMCPTransport(upstream)

    return AggregateHub(transport_factory=factory)


def _endpoints(*servers: Server) -> SimpleNamespace:
    """Fake supervisor: every given server is 'running' on a synthetic endpoint whose
    host doubles as the transport-factory key."""
    return SimpleNamespace(
        running_endpoints=lambda: [
            (s.id, s.slug, f"up-{s.slug}", 49000 + i) for i, s in enumerate(servers)
        ]
    )


def _mk_server(session: Session, name: str, **kw) -> SimpleNamespace:
    """Create a (disabled) server row and capture id/slug while the session is open
    (the ORM instance detaches when the caller's session closes)."""
    srv = service.create_server(
        session, name=name, runner="npx", command="npx", args=["-y", "pkg"], **kw
    )
    return SimpleNamespace(id=srv.id, slug=srv.slug)


def _mounted(asgi_app, prefix: str = "/s/all"):
    """Simulate Starlette's Mount scope mutation (full ``path`` + extended
    ``root_path`` — the shape the inner app sees behind ``app.mount``)."""

    async def wrapper(scope, receive, send):
        scope = dict(scope)
        scope["root_path"] = prefix
        scope["path"] = prefix + scope["path"]
        await asgi_app(scope, receive, send)

    return wrapper


async def _rpc(asgi_app, method: str, params: dict | None = None, session_id: str | None = None):
    """One JSON-RPC call against the aggregate's Streamable-HTTP app; returns
    (result-or-error dict, mcp-session-id header). Handles SSE and JSON bodies."""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agg") as client:
        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=headers,
        )
    assert r.status_code == 200, r.text
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        data_lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
        payload = json.loads(data_lines[-1].removeprefix("data:").strip())
    else:
        payload = json.loads(body)
    return payload, r.headers.get("mcp-session-id")


_INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"},
}


async def _list_tool_names(asgi_app) -> list[str]:
    _, session_id = await _rpc(asgi_app, "initialize", _INIT_PARAMS)
    payload, _ = await _rpc(asgi_app, "tools/list", session_id=session_id)
    assert "result" in payload, payload
    return sorted(t["name"] for t in payload["result"]["tools"])


# --- hub: mount semantics + membership ---------------------------------------- #


async def test_hub_mounts_running_servers_with_namespaced_tools(clean_settings):
    """All running servers' tools surface under slug namespaces, and calls route to
    the right upstream."""
    _write_settings(unified_endpoint=True)
    with Session(get_engine()) as session:
        a = _mk_server(session, "Alpha")
        b = _mk_server(session, "My Beta")  # hyphenated slug: my-beta
    try:
        hub = _hub_for({"up-alpha": _make_upstream("alpha", "hello"),
                        "up-my-beta": _make_upstream("beta", "world")})
        await hub.sync(_endpoints(a, b))
        assert hub.app is not None
        assert await _list_tool_names(hub.app) == ["alpha_hello", "my-beta_world"]
        # and behind the /s/all mount scope (full path + root_path) it routes the same
        assert await _list_tool_names(_mounted(hub.app)) == ["alpha_hello", "my-beta_world"]

        _, session_id = await _rpc(hub.app, "initialize", _INIT_PARAMS)
        payload, _ = await _rpc(
            hub.app, "tools/call",
            {"name": "my-beta_world", "arguments": {"q": "x"}},
            session_id=session_id,
        )
        assert payload["result"]["content"][0]["text"] == "beta:x"
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


async def test_hub_disabled_setting_tears_down(clean_settings):
    _write_settings(unified_endpoint=True)
    with Session(get_engine()) as session:
        a = _mk_server(session, "Alpha2")
    try:
        hub = _hub_for({"up-alpha2": _make_upstream("alpha", "hello")})
        await hub.sync(_endpoints(a))
        assert hub.app is not None
        _write_settings(unified_endpoint=False)
        await hub.sync(_endpoints(a))
        assert hub.app is None
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)


async def test_hub_selection_subset_and_unknown_ids(clean_settings):
    """unified_servers=[ids] restricts membership; a stale id (deleted server) is
    ignored rather than an error."""
    with Session(get_engine()) as session:
        a = _mk_server(session, "Sel A")
        b = _mk_server(session, "Sel B")
    _write_settings(unified_endpoint=True, unified_servers=[a.id, "gone-id"])
    try:
        hub = _hub_for({"up-sel-a": _make_upstream("a", "one"),
                        "up-sel-b": _make_upstream("b", "two")})
        await hub.sync(_endpoints(a, b))
        assert await _list_tool_names(hub.app) == ["sel-a_one"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


async def test_hub_excludes_stricter_auth_servers_when_default_is_none(clean_settings):
    """Anti-downgrade: with default auth 'none' the aggregate is unauthenticated, so a
    bearer-protected server must not be mounted (its tools would bypass auth)."""
    _write_settings(unified_endpoint=True, default_auth_provider="none")
    with Session(get_engine()) as session:
        open_srv = _mk_server(session, "Open One")
        locked = _mk_server(session, "Locked One", auth_provider="bearer")
    try:
        hub = _hub_for({"up-open-one": _make_upstream("o", "free"),
                        "up-locked-one": _make_upstream("l", "secret")})
        await hub.sync(_endpoints(open_srv, locked))
        assert await _list_tool_names(hub.app) == ["open-one_free"]

        # under default 'bearer' the aggregate itself requires an all-scoped token,
        # which authorizes every server — so both are mounted
        _write_settings(default_auth_provider="bearer")
        await hub.sync(_endpoints(open_srv, locked))
        names = await _list_tool_names(hub.app)
        assert names == ["locked-one_secret", "open-one_free"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, open_srv.id)
            repo.delete_server(session, locked.id)


async def test_hub_unchanged_topology_is_a_noop_swap(clean_settings):
    _write_settings(unified_endpoint=True)
    with Session(get_engine()) as session:
        a = _mk_server(session, "Stable")
    try:
        hub = _hub_for({"up-stable": _make_upstream("s", "t")})
        sup = _endpoints(a)
        await hub.sync(sup)
        first = hub.app
        await hub.sync(sup)
        assert hub.app is first  # same instance kept — no rebuild churn
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)


async def test_hub_dead_upstream_is_skipped_not_fatal(clean_settings):
    """A mounted-but-dead bridge (crashed between reconciles) only loses its own
    namespace — list_tools still returns the healthy servers' tools."""
    _write_settings(unified_endpoint=True)
    with Session(get_engine()) as session:
        ok = _mk_server(session, "Healthy")
        dead = _mk_server(session, "Dead")
    try:
        hub = _hub_for({"up-healthy": _make_upstream("h", "works"),
                        "up-dead": _BrokenTransport()})
        await hub.sync(_endpoints(ok, dead))
        assert await _list_tool_names(hub.app) == ["healthy_works"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, ok.id)
            repo.delete_server(session, dead.id)


# --- supervisor hook ----------------------------------------------------------- #


async def test_reconcile_fires_on_converged_and_guards_errors():
    from app.supervisor.supervisor import Supervisor

    sup = Supervisor()
    calls = []

    async def hook():
        calls.append(1)
        raise RuntimeError("hub bug")

    sup.on_converged = hook
    await sup.reconcile_once()  # must not raise despite the hook failing
    assert calls == [1]


# --- /s/all route -------------------------------------------------------------- #


async def _fake_inner(scope, receive, send):
    await JSONResponse(
        {"path": scope["path"], "root_path": scope.get("root_path", "")}
    )(scope, receive, send)


def _stub_hub(client: TestClient) -> None:
    """Freeze the hub with a fake inner app (and stop the reconciler's sync from
    clobbering it), the way test_proxy stubs supervisor.endpoint."""
    client.app.state.supervisor.on_converged = None
    client.app.state.aggregate._app = _fake_inner


def _mint_token(scope: str = "all") -> str:
    raw = new_token()
    with Session(get_engine()) as session:
        repo.create_token(
            session,
            Token(id=new_id(), name="t", token_hash=hash_token(raw), prefix=raw[:12], scope=scope),
        )
    return raw


def test_route_404_when_disabled():
    """Off (the default) is indistinguishable from a nonexistent slug."""
    with TestClient(app) as client:
        r = client.post("/s/all/mcp", headers=LOOPBACK)
        assert r.status_code == 404
        assert "unknown server" in r.text


def test_route_rejects_off_allowlist_host(clean_settings):
    _write_settings(unified_endpoint=True)
    with TestClient(app) as client:
        r = client.post("/s/all/mcp", headers={"host": "evil.example"})
        assert r.status_code == 403


def test_route_503_when_nothing_mounted(clean_settings):
    _write_settings(unified_endpoint=True)
    with TestClient(app) as client:
        client.app.state.supervisor.on_converged = None
        client.app.state.aggregate._app = None
        r = client.post("/s/all/mcp", headers=LOOPBACK)
        assert r.status_code == 503
        assert "not running" in r.text


def test_route_delegates_with_mount_scope(clean_settings):
    """The dispatcher delegates to the hub's app with Mount's scope shape: full path,
    root_path extended by /s/all — so the inner app's routing resolves /mcp."""
    _write_settings(unified_endpoint=True)
    with TestClient(app) as client:
        _stub_hub(client)
        r = client.post("/s/all/mcp", headers=LOOPBACK)
        assert r.status_code == 200
        body = r.json()
        assert body["root_path"].endswith("/s/all")
        # what Starlette routing matches against (get_route_path) is the remainder
        assert body["path"].removeprefix(body["root_path"]) == "/mcp"


def test_route_bearer_matrix(clean_settings):
    """Default 'bearer': no token 401; a per-server token 403 (can't authorize the
    bundle); an all-scoped token passes auth and reaches the hub."""
    _write_settings(unified_endpoint=True, default_auth_provider="bearer")
    with TestClient(app) as client:
        _stub_hub(client)
        server_scoped = _mint_token(scope=new_id())  # some other server's token
        all_scoped = _mint_token(scope="all")

        r = client.post("/s/all/mcp", headers=LOOPBACK)
        assert r.status_code == 401

        r = client.post(
            "/s/all/mcp", headers={**LOOPBACK, "authorization": f"Bearer {server_scoped}"}
        )
        assert r.status_code == 403

        r = client.post(
            "/s/all/mcp", headers={**LOOPBACK, "authorization": f"Bearer {all_scoped}"}
        )
        assert r.status_code == 200


# --- settings: registry validation + API surface -------------------------------- #


def test_unified_settings_validation(clean_settings):
    with Session(get_engine()) as session:
        with pytest.raises(ValueError):
            runtime_settings.write(session, {"unified_endpoint": "yes"})
        with pytest.raises(ValueError):
            runtime_settings.write(session, {"unified_servers": "some"})
        with pytest.raises(ValueError):
            runtime_settings.write(session, {"unified_servers": [1, 2]})
        # dedupe, order kept
        runtime_settings.write(session, {"unified_servers": ["b", "a", "b"]})
        assert runtime_settings.unified_servers(session) == ["b", "a"]
        runtime_settings.write(session, {"unified_servers": "all"})
        assert runtime_settings.unified_servers(session) == "all"


def test_settings_api_round_trip_and_url(clean_settings):
    with TestClient(app) as client:
        r = client.get("/api/settings", headers=LOOPBACK)
        assert r.json()["unified_endpoint"] is False
        assert r.json()["unified_endpoint_url"] is None

        r = client.patch("/api/settings", json={"unified_endpoint": True}, headers=LOOPBACK)
        assert r.status_code == 200
        body = r.json()
        assert body["unified_endpoint"] is True
        assert body["unified_endpoint_url"].endswith("/s/all/mcp")

        r = client.patch(
            "/api/settings", json={"unified_servers": ["id-1"]}, headers=LOOPBACK
        )
        assert r.json()["unified_servers"] == ["id-1"]

        # StrictBool / Literal["all"]|list at the API boundary -> 422, never coerced
        r = client.patch("/api/settings", json={"unified_endpoint": "yes"}, headers=LOOPBACK)
        assert r.status_code == 422
        r = client.patch("/api/settings", json={"unified_servers": "some"}, headers=LOOPBACK)
        assert r.status_code == 422

        r = client.patch("/api/settings", json={"unified_endpoint": False}, headers=LOOPBACK)
        assert r.json()["unified_endpoint_url"] is None


# --- slug reservation + migration ------------------------------------------------ #


def test_reserved_all_slug_is_not_assigned():
    from sqlmodel import SQLModel, create_engine

    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        a = service.create_server(session, name="all", runner="npx", command="npx")
        assert a.slug == "all-2"  # "all" stays free for the unified endpoint mount
        with pytest.raises(ValueError):
            service.update_server(session, a.id, {"slug": "all"})


def test_normalize_reserved_slugs_renames_legacy_all():
    """A pre-reservation row slugged "all" is renamed at boot (else the /s/all mount
    would silently shadow it)."""
    from sqlmodel import SQLModel, create_engine

    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        legacy = Server(
            id=new_id(), slug="all", name="Legacy", args=[], env={}, config_hash="x"
        )
        repo.save_server(session, legacy)
        assert service.normalize_reserved_slugs(session) == 1
        assert repo.get_server(session, legacy.id).slug == "all-2"
        # idempotent
        assert service.normalize_reserved_slugs(session) == 0
