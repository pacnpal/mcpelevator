"""Group registry tests — the registry, the group hub, its /g/<name> dispatcher,
startup validation, and per-group bearer scoping.

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

from app.db import get_engine, repo
from app.db.models import Server, Token
from app.groups import registry
from app.groups.hub import GroupHub
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token

LOOPBACK = {"host": "127.0.0.1"}


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
        runtime_settings.write(session, {"groups": {}, "default_auth_provider": "none"})


def _write_groups(groups: dict, **changes) -> None:
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"groups": groups, **changes})


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


def _hub_for(upstreams: dict[str, FastMCP | FastMCPTransport]) -> GroupHub:
    """Hub whose transport factory resolves the synthetic bridge URLs to in-memory
    upstreams, keyed by host (see ``_endpoints``)."""

    def factory(url: str):
        host = url.removeprefix("http://").split(":", 1)[0]
        upstream = upstreams[host]
        return upstream if isinstance(upstream, FastMCPTransport) else FastMCPTransport(upstream)

    return GroupHub(transport_factory=factory)


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


async def _rpc(asgi_app, method: str, params: dict | None = None, session_id: str | None = None):
    """One JSON-RPC call against a group's Streamable-HTTP app; returns
    (result-or-error dict, mcp-session-id header). Handles SSE and JSON bodies."""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://grp") as client:
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


# --- registry: resolution + validation ---------------------------------------- #


def test_registry_resolve_wildcard_and_explicit(clean_settings):
    with Session(get_engine()) as session:
        a = _mk_server(session, "Reg A")
        b = _mk_server(session, "Reg B")
    try:
        _write_groups({"all": "*", "pick": [a.id]})
        with Session(get_engine()) as session:
            assert set(registry.resolve(session, "all")) == {a.id, b.id}
            assert registry.resolve(session, "pick") == [a.id]
            assert registry.resolve(session, "nope") is None  # unknown group -> None
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


def test_registry_write_rejects_unknown_member(clean_settings):
    """A group referencing a nonexistent server is rejected at write time, naming the
    offending group and server id."""
    with Session(get_engine()) as session:
        with pytest.raises(registry.UnknownMemberError) as exc:
            registry.write_group(session, "bad", ["ghost-id"])
    assert exc.value.group == "bad"
    assert exc.value.server_id == "ghost-id"
    assert "bad" in str(exc.value) and "ghost-id" in str(exc.value)


def test_registry_validate_at_startup_fails_loudly(clean_settings):
    """A registry with a dangling member id (only reachable via a hand-edited DB, since
    writes validate and deletes prune) fails the boot with an error naming both."""
    # write a clean group, then delete its member out from under it to simulate the
    # inconsistent-config state validate_at_startup guards against.
    with Session(get_engine()) as session:
        srv = _mk_server(session, "Startup Victim")
    _write_groups({"team": [srv.id]})
    with Session(get_engine()) as session:
        repo.delete_server(session, srv.id)  # repo delete does NOT prune the registry
        with pytest.raises(RuntimeError) as exc:
            registry.validate_at_startup(session)
    msg = str(exc.value)
    assert "team" in msg and srv.id in msg


def test_registry_prune_server_drops_from_explicit_lists(clean_settings):
    with Session(get_engine()) as session:
        a = _mk_server(session, "Prune A")
        b = _mk_server(session, "Prune B")
    try:
        _write_groups({"wild": "*", "pair": [a.id, b.id]})
        with Session(get_engine()) as session:
            registry.prune_server(session, a.id)
            reg = registry.read(session)
        assert reg["pair"] == [b.id]  # a dropped from the explicit list
        assert reg["wild"] == "*"  # wildcard untouched
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


def test_registry_settings_validation(clean_settings):
    with Session(get_engine()) as session:
        with pytest.raises(ValueError):  # bad name grammar
            runtime_settings.write(session, {"groups": {"Bad Name": "*"}})
        with pytest.raises(ValueError):  # member value neither "*" nor a list
            runtime_settings.write(session, {"groups": {"g": "some"}})
        with pytest.raises(ValueError):  # non-string members
            runtime_settings.write(session, {"groups": {"g": [1, 2]}})
        with pytest.raises(ValueError):  # not a mapping
            runtime_settings.write(session, {"groups": ["a", "b"]})
        # dedupe, order kept
        runtime_settings.write(session, {"groups": {"g": ["b", "a", "b"]}})
        assert runtime_settings.groups(session) == {"g": ["b", "a"]}


# --- hub: mount semantics + membership ---------------------------------------- #


async def test_hub_mounts_group_members_with_namespaced_tools(clean_settings):
    """A group's running members' tools surface under slug namespaces, and calls route
    to the right upstream."""
    with Session(get_engine()) as session:
        a = _mk_server(session, "Alpha")
        b = _mk_server(session, "My Beta")  # hyphenated slug: my-beta
    _write_groups({"all": "*"})
    try:
        hub = _hub_for({"up-alpha": _make_upstream("alpha", "hello"),
                        "up-my-beta": _make_upstream("beta", "world")})
        await hub.sync(_endpoints(a, b))
        inner = hub.app_for("all")
        assert inner is not None
        assert await _list_tool_names(inner) == ["alpha_hello", "my-beta_world"]

        _, session_id = await _rpc(inner, "initialize", _INIT_PARAMS)
        payload, _ = await _rpc(
            inner, "tools/call",
            {"name": "my-beta_world", "arguments": {"q": "x"}},
            session_id=session_id,
        )
        assert payload["result"]["content"][0]["text"] == "beta:x"
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


async def test_hub_explicit_membership_is_a_subset(clean_settings):
    """A group with an explicit member list bundles only those servers."""
    with Session(get_engine()) as session:
        a = _mk_server(session, "Sel A")
        b = _mk_server(session, "Sel B")
    _write_groups({"pick": [a.id]})
    try:
        hub = _hub_for({"up-sel-a": _make_upstream("a", "one"),
                        "up-sel-b": _make_upstream("b", "two")})
        await hub.sync(_endpoints(a, b))
        assert await _list_tool_names(hub.app_for("pick")) == ["sel-a_one"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


async def test_hub_multiple_groups_coexist(clean_settings):
    """Two groups mount independent bundles from the same running topology."""
    with Session(get_engine()) as session:
        a = _mk_server(session, "Multi A")
        b = _mk_server(session, "Multi B")
    _write_groups({"ga": [a.id], "gb": [b.id]})
    try:
        hub = _hub_for({"up-multi-a": _make_upstream("a", "one"),
                        "up-multi-b": _make_upstream("b", "two")})
        await hub.sync(_endpoints(a, b))
        assert await _list_tool_names(hub.app_for("ga")) == ["multi-a_one"]
        assert await _list_tool_names(hub.app_for("gb")) == ["multi-b_two"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)
            repo.delete_server(session, b.id)


async def test_hub_empty_group_serves_empty_bundle(clean_settings):
    """A group with no running members is a valid, tool-less bundle (documented
    behavior): initialize works and tools/list is empty — not a 404 or 503."""
    _write_groups({"empty": []})
    hub = _hub_for({})
    try:
        await hub.sync(_endpoints())  # nothing running
        inner = hub.app_for("empty")
        assert inner is not None
        assert await _list_tool_names(inner) == []
        await hub.close()
    finally:
        pass


async def test_hub_removed_group_is_torn_down(clean_settings):
    with Session(get_engine()) as session:
        a = _mk_server(session, "Gone Group")
    _write_groups({"g": [a.id]})
    try:
        hub = _hub_for({"up-gone-group": _make_upstream("a", "hello")})
        await hub.sync(_endpoints(a))
        assert hub.app_for("g") is not None
        _write_groups({})  # group removed from the registry
        await hub.sync(_endpoints(a))
        assert hub.app_for("g") is None
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)


async def test_hub_excludes_stricter_auth_members_when_default_is_none(clean_settings):
    """Anti-downgrade: with default auth 'none' a group is unauthenticated, so a
    bearer-protected member must not be mounted (its tools would bypass auth)."""
    with Session(get_engine()) as session:
        open_srv = _mk_server(session, "Open One")
        locked = _mk_server(session, "Locked One", auth_provider="bearer")
    _write_groups({"all": "*"}, default_auth_provider="none")
    try:
        hub = _hub_for({"up-open-one": _make_upstream("o", "free"),
                        "up-locked-one": _make_upstream("l", "secret")})
        await hub.sync(_endpoints(open_srv, locked))
        assert await _list_tool_names(hub.app_for("all")) == ["open-one_free"]

        # under default 'bearer' the group itself requires a matching token, which
        # authorizes every member — so both are mounted
        _write_groups({"all": "*"}, default_auth_provider="bearer")
        await hub.sync(_endpoints(open_srv, locked))
        assert await _list_tool_names(hub.app_for("all")) == ["locked-one_secret", "open-one_free"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, open_srv.id)
            repo.delete_server(session, locked.id)


async def test_hub_unchanged_topology_is_a_noop_swap(clean_settings):
    with Session(get_engine()) as session:
        a = _mk_server(session, "Stable")
    _write_groups({"g": "*"})
    try:
        hub = _hub_for({"up-stable": _make_upstream("s", "t")})
        sup = _endpoints(a)
        await hub.sync(sup)
        first = hub.app_for("g")
        await hub.sync(sup)
        assert hub.app_for("g") is first  # same instance kept — no rebuild churn
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)


async def test_hub_dead_upstream_is_skipped_not_fatal(clean_settings):
    """A mounted-but-dead bridge (crashed between reconciles) only loses its own
    namespace — list_tools still returns the healthy members' tools."""
    with Session(get_engine()) as session:
        ok = _mk_server(session, "Healthy")
        dead = _mk_server(session, "Dead")
    _write_groups({"g": "*"})
    try:
        hub = _hub_for({"up-healthy": _make_upstream("h", "works"),
                        "up-dead": _BrokenTransport()})
        await hub.sync(_endpoints(ok, dead))
        assert await _list_tool_names(hub.app_for("g")) == ["healthy_works"]
        await hub.close()
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, ok.id)
            repo.delete_server(session, dead.id)


def test_internal_hop_never_forwards_authorization():
    """A group's bearer token authorizes the whole bundle; the hub's internal hop to
    the loopback bridges must not propagate it (a remote-runner bridge would forward it
    once more, leaking it to arbitrary upstreams)."""
    from fastmcp.server.providers.proxy import ProxyClient

    from app.groups.hub import _default_transport

    # premise: fastmcp's ProxyClient turns caller-credential forwarding ON for HTTP
    # transports — if this ever changes, the override below is dead code to revisit
    plain = ProxyClient(_default_transport("http://127.0.0.1:1/mcp"))
    assert plain.transport.forward_incoming_headers is True

    captured = []

    def factory(url: str):
        transport = _default_transport(url)
        captured.append(transport)
        return transport

    GroupHub(transport_factory=factory)._make_proxy("s", "http://127.0.0.1:1/mcp")
    assert captured[0].forward_incoming_headers is False


# --- /g/<name> route ----------------------------------------------------------- #


async def _fake_inner(scope, receive, send):
    await JSONResponse(
        {"path": scope["path"], "root_path": scope.get("root_path", "")}
    )(scope, receive, send)


class _NoopRunner:
    """Stand-in for _AppRunner in route tests: teardown (lifespan shutdown) awaits
    close(), so it must be an awaitable no-op."""

    async def close(self) -> None:
        pass


def _stub_group(client: TestClient, name: str) -> None:
    """Freeze a group with a fake inner app (and stop the reconciler's sync from
    clobbering it), the way test_proxy stubs supervisor.endpoint."""
    from app.groups.hub import _Instance

    client.app.state.supervisor.on_converged = None
    hub = client.app.state.groups
    hub._instances[name] = _Instance(_fake_inner, _NoopRunner(), frozenset())


def _mint_token(scope: str = "all") -> str:
    raw = new_token()
    with Session(get_engine()) as session:
        repo.create_token(
            session,
            Token(id=new_id(), name="t", token_hash=hash_token(raw), prefix=raw[:12], scope=scope),
        )
    return raw


def test_route_404_for_unknown_group(clean_settings):
    """An unknown group name is a clean 404 (never a 500), same body shape as an
    unknown /s slug."""
    with TestClient(app) as client:
        r = client.post("/g/nope/mcp", headers=LOOPBACK)
        assert r.status_code == 404
        assert "unknown group" in r.text


def test_route_rejects_off_allowlist_host(clean_settings):
    _write_groups({"g": "*"})
    with TestClient(app) as client:
        r = client.post("/g/g/mcp", headers={"host": "evil.example"})
        assert r.status_code == 403


def test_route_503_when_group_not_built(clean_settings):
    """The group exists in the registry but the hub hasn't built it yet (transient)."""
    _write_groups({"g": "*"})
    with TestClient(app) as client:
        client.app.state.supervisor.on_converged = None
        client.app.state.groups._instances.pop("g", None)
        r = client.post("/g/g/mcp", headers=LOOPBACK)
        assert r.status_code == 503
        assert "not ready" in r.text


def test_route_delegates_with_mount_scope(clean_settings):
    """The dispatcher delegates to the group's app with Mount's scope shape: full path,
    root_path extended by /g/<name> — so the inner app's routing resolves /mcp."""
    _write_groups({"team": "*"})
    with TestClient(app) as client:
        _stub_group(client, "team")
        r = client.post("/g/team/mcp", headers=LOOPBACK)
        assert r.status_code == 200
        body = r.json()
        assert body["root_path"].endswith("/g/team")
        # what Starlette routing matches against (get_route_path) is the remainder
        assert body["path"].removeprefix(body["root_path"]) == "/mcp"


def test_route_bearer_matrix(clean_settings):
    """Default 'bearer': no token 401; a per-server token 403 (can't authorize the
    bundle); a group-scoped token passes for its own group and reaches the hub."""
    _write_groups({"team": "*"}, default_auth_provider="bearer")
    with TestClient(app) as client:
        _stub_group(client, "team")
        server_scoped = _mint_token(scope=new_id())  # some server's token
        group_scoped = _mint_token(scope="group:team")
        all_scoped = _mint_token(scope="all")

        r = client.post("/g/team/mcp", headers=LOOPBACK)
        assert r.status_code == 401

        r = client.post("/g/team/mcp", headers={**LOOPBACK, "authorization": f"Bearer {server_scoped}"})
        assert r.status_code == 403

        r = client.post("/g/team/mcp", headers={**LOOPBACK, "authorization": f"Bearer {group_scoped}"})
        assert r.status_code == 200

        r = client.post("/g/team/mcp", headers={**LOOPBACK, "authorization": f"Bearer {all_scoped}"})
        assert r.status_code == 200


def test_route_group_token_rejected_on_other_group(clean_settings):
    """A token scoped to group A hitting group B is rejected exactly like a
    wrong-server token (403), never accepted."""
    _write_groups({"a": "*", "b": "*"}, default_auth_provider="bearer")
    with TestClient(app) as client:
        _stub_group(client, "a")
        _stub_group(client, "b")
        a_token = _mint_token(scope="group:a")

        r = client.post("/g/a/mcp", headers={**LOOPBACK, "authorization": f"Bearer {a_token}"})
        assert r.status_code == 200  # its own group
        r = client.post("/g/b/mcp", headers={**LOOPBACK, "authorization": f"Bearer {a_token}"})
        assert r.status_code == 403  # a different group — rejected


def test_patch_default_auth_downgrade_resyncs_group_before_returning(clean_settings):
    """PATCHing default_auth_provider bearer->none must not leave a window where a
    group's OLD mounted set (which may include bearer-only members) is served under the
    NEW unauthenticated default — the handler resyncs the hub before returning."""
    with Session(get_engine()) as session:
        locked = _mk_server(session, "Locked Down", auth_provider="bearer")
    _write_groups({"all": "*"}, default_auth_provider="bearer")
    admin = _mint_token(scope="all")
    auth = {**LOOPBACK, "authorization": f"Bearer {admin}"}
    try:
        with TestClient(app) as client:
            client.app.state.supervisor.on_converged = None  # only PATCH-time syncs
            client.app.state.supervisor.running_endpoints = lambda: [
                (locked.id, locked.slug, "127.0.0.1", 49999)
            ]
            # prime under bearer: the locked member is mounted (mounting is lazy — no
            # connection is made, so the dead port is irrelevant)
            client.app.state.groups._instances.pop("all", None)
            r = client.patch(
                "/api/settings", json={"default_auth_provider": "bearer"}, headers=auth
            )
            assert r.status_code == 200
            hub = client.app.state.groups
            assert hub._instances["all"].key == frozenset({(locked.slug, "127.0.0.1", 49999)})

            # downgrade: by the time the PATCH returns, the bearer-only member is out
            r = client.patch(
                "/api/settings", json={"default_auth_provider": "none"}, headers=auth
            )
            assert r.status_code == 200
            assert hub._instances["all"].key == frozenset()  # excluded, no async gap
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, locked.id)


def test_patch_server_auth_tightening_resyncs_group_before_returning(clean_settings):
    """PATCHing a mounted member from none/inherit to explicit bearer (under default
    'none') must drop it from its groups before the response returns — not on the next
    reconcile — or its tools stay reachable unauthenticated in the gap."""
    with Session(get_engine()) as session:
        srv = _mk_server(session, "Tightened")
    _write_groups({"all": "*"}, default_auth_provider="none")
    try:
        with TestClient(app) as client:
            client.app.state.supervisor.on_converged = None  # only handler-time syncs
            client.app.state.supervisor.running_endpoints = lambda: [
                (srv.id, srv.slug, "127.0.0.1", 49998)
            ]
            client.app.state.groups._instances.pop("all", None)
            # a server PATCH touching a resync field (mcp_http) builds the initial mounted
            # set through the same _resync_groups path the tightening below exercises
            r = client.patch(
                f"/api/servers/{srv.id}", json={"mcp_http": True}, headers=LOOPBACK
            )
            assert r.status_code == 200
            hub = client.app.state.groups
            assert hub._instances["all"].key == frozenset({(srv.slug, "127.0.0.1", 49998)})

            r = client.patch(
                f"/api/servers/{srv.id}", json={"auth_provider": "bearer"}, headers=LOOPBACK
            )
            assert r.status_code == 200
            assert hub._instances["all"].key == frozenset()  # dropped, no async gap
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, srv.id)


# --- API surface + no /s/all survives ------------------------------------------ #


def test_groups_api_crud_round_trip(clean_settings):
    with Session(get_engine()) as session:
        a = _mk_server(session, "Api A")
    try:
        with TestClient(app) as client:
            client.app.state.supervisor.on_converged = None
            # create
            r = client.put("/api/groups/team", json={"members": [a.id]}, headers=LOOPBACK)
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "team" and body["members"] == [a.id]
            assert body["url"].endswith("/g/team/mcp")
            # list
            r = client.get("/api/groups", headers=LOOPBACK)
            assert [g["name"] for g in r.json()] == ["team"]
            # unknown member -> 400
            r = client.put("/api/groups/bad", json={"members": ["ghost"]}, headers=LOOPBACK)
            assert r.status_code == 400
            # bad name grammar -> 400
            r = client.put("/api/groups/Bad Name", json={"members": "*"}, headers=LOOPBACK)
            assert r.status_code == 400
            # delete
            r = client.delete("/api/groups/team", headers=LOOPBACK)
            assert r.status_code == 204
            r = client.delete("/api/groups/team", headers=LOOPBACK)
            assert r.status_code == 404
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, a.id)


def test_token_scope_accepts_group_and_rejects_unknown(clean_settings):
    _write_groups({"team": "*"})
    with TestClient(app) as client:
        r = client.post("/api/tokens", json={"name": "t", "scope": "group:team"}, headers=LOOPBACK)
        assert r.status_code == 201
        assert r.json()["scope"] == "group:team"
        r = client.post("/api/tokens", json={"name": "t", "scope": "group:ghost"}, headers=LOOPBACK)
        assert r.status_code == 400


def test_no_s_all_route_survives():
    """Clean break: nothing serves /s/all any more. With no server slugged "all", the
    proxy returns its generic unknown-server 404 (not a reserved aggregate mount)."""
    import app.main as main_module

    # no aggregate module remains
    with pytest.raises(ModuleNotFoundError):
        __import__("app.aggregate")
    # and no route path mentions /s/all
    for route in main_module.app.routes:
        assert getattr(route, "path", "") != "/s/all"
        assert "/s/all" not in getattr(route, "path", "")


def test_s_all_is_just_an_ordinary_server(clean_settings):
    """"all" is a normal slug now: a server slugged "all" serves at /s/all with the
    generic proxy semantics (503 when not running), proving no special mount shadows it."""
    with Session(get_engine()) as session:
        srv = service.create_server(session, name="all", runner="npx", command="npx")
        assert srv.slug == "all"
        sid = srv.id
    try:
        with TestClient(app) as client:
            r = client.post("/s/all/mcp", headers=LOOPBACK)
            # a real (but not-running) server: 503, NOT the old aggregate's behavior
            assert r.status_code == 503
            assert "not running" in r.text
    finally:
        with Session(get_engine()) as session:
            repo.delete_server(session, sid)
