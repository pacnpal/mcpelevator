"""Usage accounting: attribution rules, the recorder's flush, the two recording
sites (the /s proxy and the /g dispatcher), the read API, retention and cleanup.

The bridge subprocess is never spawned — the proxy forwards to an in-process fake
backend and the group dispatcher delegates to a fake inner app, exactly like
``test_proxy`` / ``test_groups`` — so the counting rules are exercised end to end
without a real MCP server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from conftest import LOOPBACK, create_server

from app import usage
from app.db import get_engine, repo
from app.db.models import Server, UsageBucket
from app.main import app
from app.registry import settings as runtime_settings
from app.usage import attribution, recorder, stats


# --- fixtures / helpers ------------------------------------------------------- #


# The unit-level recorder/stats tests count against this id rather than standing up
# a server through the API. The row has to EXIST, though: the flush drops counts for
# servers that don't (see repo.bump_usage), which is what stops a deleted server's
# rows from coming back.
SYNTHETIC_ID = "srv"


@pytest.fixture(autouse=True)
def _clean_usage():
    """Counters are process-global; start and finish every test with none pending
    and no stored rows, so ordering can't leak counts between tests."""
    recorder.reset()
    _clear_rows()
    _ensure_synthetic_server()
    yield
    recorder.reset()
    _clear_rows()
    with Session(get_engine()) as session:
        repo.delete_server(session, SYNTHETIC_ID)


def _ensure_synthetic_server() -> None:
    with Session(get_engine()) as session:
        if repo.get_server(session, SYNTHETIC_ID) is None:
            repo.create_server(
                session,
                Server(
                    id=SYNTHETIC_ID,
                    slug="synthetic-usage-target",
                    name="Synthetic usage target",
                    args=[],
                    env={},
                ),
            )


def _clear_rows() -> None:
    with Session(get_engine()) as session:
        for row in session.exec(select(UsageBucket)).all():
            session.delete(row)
        session.commit()


async def _upstream_echo(request):
    await request.body()
    return JSONResponse({"ok": True})


_upstream = Starlette(routes=[Route("/{path:path}", _upstream_echo, methods=["GET", "POST"])])


def _point_proxy_at_upstream(client: TestClient) -> None:
    client.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_upstream))
    client.app.state.supervisor.endpoint = lambda slug: ("backend", 9000)


def _call(tool: str, arguments: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }


def _rows(server_id: str) -> dict[str, int]:
    """Stored calls per tool for one server (flushing anything still pending)."""
    recorder.flush_sync()
    with Session(get_engine()) as session:
        return {
            row.tool: row.calls
            for row in repo.usage_since(
                session, server_id, datetime.now(timezone.utc) - timedelta(days=1)
            )
        }


# --- attribution rules (pure) ------------------------------------------------- #


def test_tools_from_body_names_the_called_tool():
    assert attribution.tools_from_body(json.dumps(_call("search")).encode()) == ["search"]


def test_tools_from_body_ignores_non_call_methods():
    for method in ("initialize", "tools/list", "notifications/initialized", "ping"):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}).encode()
        assert attribution.tools_from_body(body) == []


def test_tools_from_body_counts_each_element_of_a_batch():
    body = json.dumps([_call("a"), {"method": "tools/list"}, _call("b")]).encode()
    assert attribution.tools_from_body(body) == ["a", "b"]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b'"a string"',
        json.dumps({"method": "tools/call"}).encode(),  # no params
        json.dumps({"method": "tools/call", "params": "nope"}).encode(),
        json.dumps({"method": "tools/call", "params": {"name": 7}}).encode(),
        json.dumps({"method": "tools/call", "params": {"name": ""}}).encode(),
    ],
)
def test_tools_from_body_never_raises_on_junk(body):
    """Usage accounting must never be able to fail a request it only observes."""
    assert attribution.tools_from_body(body) == []


def test_oversized_body_is_not_parsed():
    huge = json.dumps(_call("search", {"blob": "x" * attribution.MAX_PARSE_BYTES})).encode()
    assert len(huge) > attribution.MAX_PARSE_BYTES
    assert attribution.tools_from_body(huge) == []


def test_deeply_nested_body_does_not_raise():
    """json.loads raises RecursionError — not a ValueError — on a payload nested
    deeply enough to exhaust the parser's stack, and it can be far under the size
    cap. Observing a request must never be able to fail it."""
    body = b"[" * 2000 + b"0" + b"]" * 2000
    assert len(body) < attribution.MAX_PARSE_BYTES
    assert attribution.tools_from_body(body) == []


def test_absurd_tool_names_are_refused():
    """The name is client-controlled, so what gets past attribution bounds what a
    counter row can hold."""
    long_name = "x" * (attribution.MAX_TOOL_NAME + 1)
    assert attribution.tools_from_body(json.dumps(_call(long_name)).encode()) == []
    ok = "x" * attribution.MAX_TOOL_NAME
    assert attribution.tools_from_body(json.dumps(_call(ok)).encode()) == [ok]


def test_a_batch_cannot_mint_unbounded_names():
    batch = [_call(f"tool{i}") for i in range(attribution.MAX_BATCH_NAMES + 20)]
    assert len(attribution.tools_from_body(json.dumps(batch).encode())) == (
        attribution.MAX_BATCH_NAMES
    )


def test_proxy_tools_reads_mcp_body_and_rest_path():
    body = json.dumps(_call("search")).encode()
    assert attribution.proxy_tools("POST", "mcp", body) == ["search"]
    assert attribution.proxy_tools("POST", "/mcp", body) == ["search"]
    # the REST mirror names the tool in the path instead
    assert attribution.proxy_tools("POST", "rest/echo", b"{}") == ["echo"]


@pytest.mark.parametrize("path", ["mcp/", "/mcp/", "mcp//", "rest/echo/"])
def test_a_trailing_slash_is_a_redirect_not_a_call(path):
    """The bridge registers `/mcp` and `/rest/<tool>` exactly, so a trailing
    slash is a 307 back to them — nothing is invoked.

    Counting it would charge the redirect AND the request the client follows it
    with, landing one call twice; a client that ignores the redirect would be
    charged for a request nothing served. Such a request still counts as plain
    traffic, just not against a tool."""
    body = json.dumps(_call("search")).encode()
    assert attribution.proxy_tools("POST", path, body) == []


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
def test_only_a_post_invokes_an_mcp_tool(method):
    """A GET opens the event stream and a DELETE ends the session; the rest are
    refused. None of them dispatch a JSON-RPC call, so a `tools/call`-shaped body
    sent with one invokes nothing — counting it would let a caller inflate a real
    tool's counter without ever running it."""
    body = json.dumps(_call("search")).encode()
    assert attribution.proxy_tools(method, "mcp", body) == []
    assert attribution.proxy_tools("POST", "mcp", body) == ["search"]


def test_a_rest_tool_name_is_bounded_like_a_json_rpc_one():
    """The path segment is client-chosen too, so without the cap a caller picks
    the size of a stored row."""
    ok = "e" * attribution.MAX_TOOL_NAME
    assert attribution.proxy_tools("POST", f"rest/{ok}", b"{}") == [ok]
    assert attribution.proxy_tools("POST", f"rest/{ok}e", b"{}") == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "rest/echo"),  # only a POST invokes a REST tool
        ("POST", "rest/openapi.json"),  # the generated doc, not a tool
        ("GET", "rest"),  # the REST index
        ("GET", "sse"),
        ("POST", "rest/nested/thing"),  # not a tool route shape
    ],
)
def test_proxy_tools_ignores_non_tool_routes(method, path):
    assert attribution.proxy_tools(method, path, b"{}") == []


def test_split_namespaced_prefers_the_longest_matching_slug():
    """Slugs and tool names both allow '_', so the split is resolved against actual
    members — and a slug that is a prefix of another must not steal the call."""
    slugs = ["files", "files_ro"]
    assert attribution.split_namespaced("files_ro_read_file", slugs) == ("files_ro", "read_file")
    assert attribution.split_namespaced("files_read_file", slugs) == ("files", "read_file")
    assert attribution.split_namespaced("other_tool", slugs) is None
    assert attribution.split_namespaced("files_", slugs) is None  # no tool part


# --- recorder ----------------------------------------------------------------- #


def test_record_folds_repeats_into_one_bucket():
    recorder.record("srv", ["search"])
    recorder.record("srv", ["search"])
    recorder.record("srv", ["fetch"])
    recorder.record("srv")  # non-tool traffic
    assert recorder.flush_sync() == 3  # three (server, tool, hour) rows
    assert _rows("srv") == {"search": 2, "fetch": 1, usage.NOT_A_TOOL: 1}


def test_flush_accumulates_across_flushes():
    """A second flush must ADD to the stored bucket, not overwrite it."""
    recorder.record("srv", ["search"])
    recorder.flush_sync()
    recorder.record("srv", ["search"])
    recorder.flush_sync()
    assert _rows("srv") == {"search": 2}


def test_flush_is_a_noop_without_pending_counts():
    assert recorder.flush_sync() == 0
    assert _rows("srv") == {}


def test_pending_keys_are_bounded_without_starving_real_tools():
    """A caller can name tools that were never advertised, and each distinct name
    is a key here and a row in storage — which time-based retention doesn't bound.
    New keys stop at the ceiling; keys already being tracked keep counting."""
    recorder.record("srv", ["real"])
    for i in range(recorder.MAX_PENDING_KEYS + 50):
        recorder.record("srv", [f"invented{i}"])
    recorder.record("srv", ["real"])  # an established key is never refused

    with recorder._lock:
        pending = dict(recorder._pending)
    assert len(pending) == recorder.MAX_PENDING_KEYS
    assert pending[("srv", "real", recorder.current_bucket())] == 2


def test_keys_being_written_still_count_against_the_ceiling(monkeypatch):
    """`_take` empties `_pending`, so without reserving the detached keys a client
    could fill the map to the cap again while the write runs — and a failed write
    merges the batch back, leaving twice the ceiling held. The guard exists to
    bound memory; it has to hold across the flush too."""
    detached = {}

    def slow_write(_session, batch):
        # Mid-write: the batch is detached, and new traffic arrives.
        detached.update(batch)
        for i in range(recorder.MAX_PENDING_KEYS + 50):
            recorder.record(SYNTHETIC_ID, [f"during{i}"])
        raise RuntimeError("database is locked")

    recorder.record(SYNTHETIC_ID, ["before"])
    monkeypatch.setattr(repo, "bump_usage", slow_write)
    with pytest.raises(RuntimeError):
        recorder.flush_sync()
    monkeypatch.undo()

    with recorder._lock:
        pending = dict(recorder._pending)
        inflight = dict(recorder._inflight)
    assert detached, "the write should have seen the detached batch"
    # Never more than the ceiling, batch restored on top and all.
    assert len(pending) <= recorder.MAX_PENDING_KEYS
    # The reservation is released once the write settles, either way.
    assert inflight == {}
    # The restored batch is still there — the point of restoring it.
    assert pending[(SYNTHETIC_ID, "before", recorder.current_bucket())] == 1


def test_stored_tool_names_are_capped_across_flushes():
    """The recorder's ceiling resets every flush, so it bounds memory but not
    storage: a client naming fresh tools each interval could persist rows
    forever. The cap on the WRITE is what makes it durable — and the calls are
    still counted, just folded under the plain-traffic sentinel."""
    cap = repo.MAX_TOOLS_PER_BUCKET
    # Two flushes, each well under the in-memory ceiling but together over the cap.
    for flush in range(2):
        for i in range(cap):
            recorder.record(SYNTHETIC_ID, [f"f{flush}n{i}"])
        recorder.flush_sync()

    rows = _rows(SYNTHETIC_ID)
    named = {tool: calls for tool, calls in rows.items() if tool}
    assert len(named) == cap, "a later flush must not add rows past the cap"
    # Nothing was lost: the refused names still counted as traffic.
    assert sum(rows.values()) == 2 * cap
    assert rows[""] == cap


def test_a_tool_already_stored_keeps_counting_past_the_cap():
    """The cap refuses NEW names, never an established one — a flood arriving
    later must not cost a real tool its row."""
    recorder.record(SYNTHETIC_ID, ["real"])
    recorder.flush_sync()
    for i in range(repo.MAX_TOOLS_PER_BUCKET + 10):
        recorder.record(SYNTHETIC_ID, [f"invented{i}"])
    recorder.record(SYNTHETIC_ID, ["real"])
    recorder.flush_sync()

    assert _rows(SYNTHETIC_ID)["real"] == 2


def test_overlapping_flushes_do_not_release_each_others_reservations():
    """The periodic flush and a read endpoint's explicit flush can overlap, and a
    key recorded between their detaches is reserved by BOTH.

    Driven through the internals in the exact order that interleaving produces,
    because a wall-clock race is too narrow to reproduce reliably. With one entry
    per key instead of a reference count, the first flush to settle released the
    second's reservation, and the second's failed write then dropped its counts
    as if `forget` had cancelled them."""
    key = (SYNTHETIC_ID, "search", recorder.current_bucket())

    recorder.record(SYNTHETIC_ID, ["search"])
    first = recorder._take()  # flush A detaches, reserving the key
    recorder.record(SYNTHETIC_ID, ["search"])  # a call lands between the detaches
    second = recorder._take()  # flush B detaches the SAME key
    assert first == {key: 1} and second == {key: 1}

    # A's write succeeds and settles. B still holds a reservation on the key.
    with recorder._lock:
        recorder._settle(first)
    with recorder._lock:
        assert recorder._inflight[key] == 1, "A's settle released B's reservation"

    # B's write fails; its count must come back rather than being dropped.
    recorder._restore(second)
    with recorder._lock:
        assert recorder._pending[key] == 1
        assert key not in recorder._inflight


def test_a_failed_write_keeps_the_counts_for_the_next_flush(monkeypatch):
    """The batch is detached before the write, so a transient database error must
    put it back rather than silently drop an interval of traffic."""
    recorder.record("srv", ["search"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "bump_usage", boom)
    with pytest.raises(RuntimeError):
        recorder.flush_sync()
    monkeypatch.undo()

    assert recorder.flush_sync() == 1
    assert _rows("srv") == {"search": 1}


# --- recording site: the /s proxy --------------------------------------------- #


def test_proxy_counts_a_tool_call_against_its_tool():
    with TestClient(app) as client:
        srv = create_server(client, name="usage-mcp", auth="none")
        try:
            _point_proxy_at_upstream(client)
            r = client.post(f"/s/{srv['slug']}/mcp", json=_call("search"), headers=LOOPBACK)
            assert r.status_code == 200, r.text
            assert _rows(srv["id"]) == {"search": 1}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_proxy_counts_non_tool_traffic_separately():
    """'Connected but never called a tool' has to be distinguishable from 'nothing
    ever reached this server' — that IS the rename-the-tool signal."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-other", auth="none")
        try:
            _point_proxy_at_upstream(client)
            client.post(
                f"/s/{srv['slug']}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers=LOOPBACK,
            )
            assert _rows(srv["id"]) == {usage.NOT_A_TOOL: 1}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_proxy_counts_a_rest_tool_call():
    with TestClient(app) as client:
        srv = create_server(client, name="usage-rest", auth="none")
        try:
            _point_proxy_at_upstream(client)
            client.post(f"/s/{srv['slug']}/rest/echo", json={"q": "hi"}, headers=LOOPBACK)
            assert _rows(srv["id"]) == {"echo": 1}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_proxy_records_nothing_when_no_bridge_is_running():
    """Only traffic that actually reached a bridge counts — a 503'd request never
    touched the server."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-down", auth="none")
        try:
            client.app.state.supervisor.endpoint = lambda slug: None
            r = client.post(f"/s/{srv['slug']}/mcp", json=_call("search"), headers=LOOPBACK)
            assert r.status_code == 503
            assert _rows(srv["id"]) == {}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_proxy_records_nothing_for_unauthorized_traffic():
    """The auth chokepoint runs first, so a rejected probe can't inflate a server's
    stats (the same rule idle bookkeeping follows)."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-auth", auth="bearer")
        try:
            _point_proxy_at_upstream(client)
            r = client.post(f"/s/{srv['slug']}/mcp", json=_call("search"), headers=LOOPBACK)
            assert r.status_code == 401
            assert _rows(srv["id"]) == {}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


# --- recording site: the /g group dispatcher ---------------------------------- #


async def _echo_body_inner(scope, receive, send):
    """Fake group app that echoes the body it received, so the replay of a consumed
    request stream is asserted, not assumed."""
    chunks = b""
    while True:
        message = await receive()
        chunks += message.get("body", b"")
        if not message.get("more_body"):
            break
    await JSONResponse({"body": chunks.decode()})(scope, receive, send)


@pytest.fixture(autouse=True)
def _unstub_group_hub():
    """Undo any `_stub_group` patching after each test.

    The hub is built once when the module-level ``app`` is constructed — not per
    lifespan — so a stub installed on it outlives the TestClient that set it.
    Without this, a later test that uses the same group name would be served the
    previous test's fake inner app (and its mounted set) instead of the real
    hub's."""
    hub = app.state.groups
    original = (hub.__dict__.get("app_for"), hub.__dict__.get("mounted_members"))
    yield
    for attr, value in zip(("app_for", "mounted_members"), original):
        if value is None:
            # Nothing shadowed the class method before this test — drop whatever
            # the test set so lookups fall back to the class again.
            hub.__dict__.pop(attr, None)
        else:
            setattr(hub, attr, value)


def _stub_group(
    client: TestClient, name: str, inner, mounted: dict[str, str] | None = None
) -> None:
    """Route a group to a fake inner app, and declare which members the hub is
    currently serving (``mounted``: slug -> server id), which is what attribution
    keys off.

    Patches the shared hub instance; `_unstub_group_hub` restores it after the
    test."""
    client.app.state.supervisor.on_converged = None
    hub = client.app.state.groups
    prev_app, prev_members = hub.app_for, hub.mounted_members
    hub.app_for = lambda n, _prev=prev_app: inner if n == name else _prev(n)
    hub.mounted_members = (
        lambda n, _prev=prev_members: dict(mounted or {}) if n == name else _prev(n)
    )


def test_group_call_counts_against_the_member_that_owns_the_tool():
    """A tool reached through a bundle is the same tool call a direct /s request
    would make, so it lands in the same per-server counters."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _echo_body_inner, mounted={srv["slug"]: srv["id"]})
            payload = _call(f"{srv['slug']}_search")
            r = client.post("/g/team/mcp", json=payload, headers=LOOPBACK)
            assert r.status_code == 200, r.text
            # the body still reached the inner app after being read for attribution
            assert json.loads(r.json()["body"]) == payload
            assert _rows(srv["id"]) == {"search": 1}
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {}})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


@pytest.mark.parametrize("subpath", ["mcp/", "mcp//"])
def test_group_trailing_slash_is_not_counted(subpath):
    """The bundle is mounted at `/mcp` alone, so `/mcp/` is a 307 back to it. If
    a slash-stripped match counted it, the redirect would be charged AND so would
    the request the client follows it with — one call, counted twice."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group-slash", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _echo_body_inner, mounted={srv["slug"]: srv["id"]})
            payload = _call(f"{srv['slug']}_search")
            client.post(f"/g/team/{subpath}", json=payload, headers=LOOPBACK)
            assert _rows(srv["id"]) == {}
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {}})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_usage_reads_are_not_stored_by_caches():
    """Both bodies are scoped to WHO asked — a member's totals cover only the
    servers they own — so a cached copy could be replayed to a different
    principal on a shared browser or by an intermediary."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-nostore", auth="none")
        try:
            for url in ("/api/usage", f"/api/servers/{srv['id']}/usage"):
                r = client.get(url, headers=LOOPBACK)
                assert r.status_code == 200, r.text
                assert r.headers["cache-control"] == "no-store", url
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_group_non_tool_traffic_is_not_charged_to_members():
    """initialize/tools/list fan out to every member; charging each would invent
    traffic none of them individually received."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group-list", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _echo_body_inner, mounted={srv["slug"]: srv["id"]})
            client.post(
                "/g/team/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers=LOOPBACK,
            )
            assert _rows(srv["id"]) == {}
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {}})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_group_call_to_an_unmounted_member_is_not_counted():
    """Registry membership is not the same as what the bundle serves: a member that
    is stopped, has mcp_http off, or is excluded by the anti-downgrade rule has no
    provider mounted, so the call gets a tool-not-found. Crediting it would let any
    caller inflate a server the request never reached."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group-unmounted", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            # configured member, but the hub is serving nothing for it
            _stub_group(client, "team", _echo_body_inner, mounted={})
            client.post("/g/team/mcp", json=_call(f"{srv['slug']}_search"), headers=LOOPBACK)
            assert _rows(srv["id"]) == {}
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {}})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_group_traffic_off_the_mcp_endpoint_is_not_counted():
    """The bundle is mounted at /mcp alone, so a POST anywhere else under the group
    404s there and served nothing — it must not show up as a member's call."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group-subpath", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _echo_body_inner, mounted={srv["slug"]: srv["id"]})
            client.post(
                "/g/team/not-mcp", json=_call(f"{srv['slug']}_search"), headers=LOOPBACK
            )
            assert _rows(srv["id"]) == {}
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {}})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_group_members_are_in_flight_before_the_body_is_buffered():
    """Reading a slow upload can outlast a member's idle deadline. The in-flight
    window has to open BEFORE usage buffers the body, or the sweep could stop a
    bridge under a request that was already accepted."""
    started: list[str] = []

    async def _slow_body_inner(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await JSONResponse({"ok": True})(scope, receive, send)

    with TestClient(app) as client:
        srv = create_server(client, name="usage-group-inflight", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _slow_body_inner, mounted={srv["slug"]: srv["id"]})
            supervisor = client.app.state.supervisor
            original = supervisor.request_started
            # Record the order: by the time the body is read for attribution, the
            # member must already be counted as busy.
            supervisor.request_started = lambda sid, _o=original: (
                started.append(sid),
                _o(sid),
            )[1]
            try:
                r = client.post(
                    "/g/team/mcp", json=_call(f"{srv['slug']}_search"), headers=LOOPBACK
                )
            finally:
                supervisor.request_started = original
            assert r.status_code == 200, r.text
            assert srv["id"] in started
            assert _rows(srv["id"]) == {"search": 1}
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {}})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_a_failure_in_accounting_never_fails_a_served_request(monkeypatch):
    """Accounting sits between the upstream response opening and the relay that
    closes it, so anything escaping there would leak the response and strand the
    server's in-flight count — silently disabling idle quiescence for it."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-guard", auth="none")
        try:
            _point_proxy_at_upstream(client)

            def boom(*_args, **_kwargs):
                raise RuntimeError("accounting exploded")

            monkeypatch.setattr(usage, "record", boom)
            r = client.post(f"/s/{srv['slug']}/mcp", json=_call("search"), headers=LOOPBACK)
            assert r.status_code == 200, r.text
            monkeypatch.undo()
            # The counter is lost (acceptable); the bridge is not left busy forever.
            assert client.app.state.supervisor._in_flight.get(srv["id"], 0) == 0
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


# --- read API ----------------------------------------------------------------- #


def test_usage_endpoint_shape_and_totals():
    with TestClient(app) as client:
        srv = create_server(client, name="usage-api", auth="none")
        try:
            _point_proxy_at_upstream(client)
            client.post(f"/s/{srv['slug']}/mcp", json=_call("search"), headers=LOOPBACK)
            client.post(f"/s/{srv['slug']}/mcp", json=_call("search"), headers=LOOPBACK)
            client.post(
                f"/s/{srv['slug']}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers=LOOPBACK,
            )
            r = client.get(f"/api/servers/{srv['id']}/usage?days=1", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["server_id"] == srv["id"]
            assert body["tool_calls"] == 2
            assert body["other_requests"] == 1
            assert body["tools"] == [
                {"tool": "search", "calls": 2, "last_call_at": body["tools"][0]["last_call_at"]}
            ]
            assert body["last_call_at"] is not None
            # dense hourly series for a 1-day window, ending with the current bucket
            assert body["bucket_seconds"] == 3600
            assert len(body["series"]) == 24
            assert sum(p["calls"] for p in body["series"]) == 2
            assert sum(p["other"] for p in body["series"]) == 1
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_usage_endpoint_is_empty_but_valid_for_an_unused_server():
    with TestClient(app) as client:
        srv = create_server(client, name="usage-quiet", auth="none")
        try:
            r = client.get(f"/api/servers/{srv['id']}/usage", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            body = r.json()
            assert (body["tool_calls"], body["other_requests"]) == (0, 0)
            assert body["tools"] == [] and body["last_call_at"] is None
            assert body["bucket_seconds"] == 86400 and len(body["series"]) == 7
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_usage_endpoint_404s_for_an_unknown_server():
    with TestClient(app) as client:
        assert client.get("/api/servers/nope/usage", headers=LOOPBACK).status_code == 404


def test_usage_endpoint_rejects_an_out_of_range_window():
    with TestClient(app) as client:
        srv = create_server(client, name="usage-range", auth="none")
        try:
            for days in (0, usage.MAX_DAYS + 1):
                r = client.get(f"/api/servers/{srv['id']}/usage?days={days}", headers=LOOPBACK)
                assert r.status_code == 422
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_usage_endpoint_flushes_pending_counts_first():
    """A call made seconds ago must already be visible — a stats view that lags its
    own traffic by a flush interval reads as broken."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-fresh", auth="none")
        try:
            recorder.record(srv["id"], ["search"])  # pending, never flushed
            body = client.get(f"/api/servers/{srv['id']}/usage?days=1", headers=LOOPBACK).json()
            assert body["tool_calls"] == 1
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


# --- instance-wide view (the dashboard's read model) -------------------------- #


def test_instance_usage_totals_across_servers():
    with TestClient(app) as client:
        a = create_server(client, name="usage-inst-a", auth="none")
        b = create_server(client, name="usage-inst-b", auth="none")
        try:
            recorder.record(a["id"], ["search"])
            recorder.record(a["id"], ["search"])
            recorder.record(b["id"], ["fetch"])
            recorder.record(b["id"])  # non-tool traffic
            recorder.flush_sync()
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            assert body["tool_calls"] == 3
            assert body["other_requests"] == 1
            assert body["active_servers"] == 2
            rows = {r["server_id"]: r for r in body["servers"]}
            assert rows[a["id"]]["tool_calls"] == 2 and rows[a["id"]]["other_requests"] == 0
            assert rows[b["id"]]["tool_calls"] == 1 and rows[b["id"]]["other_requests"] == 1
            assert rows[a["id"]]["slug"] == a["slug"]
            tools = {(t["slug"], t["tool"]): t for t in body["tools"]}
            assert tools[(a["slug"], "search")]["calls"] == 2
            assert tools[(b["slug"], "fetch")]["calls"] == 1
            assert sum(p["calls"] for p in body["series"]) == 3
        finally:
            for srv in (a, b):
                client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_lists_untouched_servers_at_zero():
    """"Which of my servers is nothing using?" is half of what this view answers,
    so a server with no traffic must be listed, not omitted."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-inst-quiet", auth="none")
        try:
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            row = next(r for r in body["servers"] if r["server_id"] == srv["id"])
            assert (row["tool_calls"], row["other_requests"]) == (0, 0)
            assert row["last_call_at"] is None
            assert body["active_servers"] == 0
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_includes_discovered_tools_never_called():
    """A discovered tool nothing has called is the row the operator is looking for,
    so it appears at zero rather than being absent."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-inst-known", auth="none")
        try:
            with Session(get_engine()) as session:
                repo.upsert_runtime(
                    session,
                    srv["id"],
                    state="running",
                    tools=[{"name": "used"}, {"name": "never_used"}],
                )
            recorder.record(srv["id"], ["used"])
            recorder.flush_sync()
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            rows = {t["tool"]: t for t in body["tools"] if t["server_id"] == srv["id"]}
            assert rows["used"]["calls"] == 1 and rows["used"]["known"] is True
            assert rows["never_used"]["calls"] == 0 and rows["never_used"]["known"] is True
            assert rows["never_used"]["last_call_at"] is None
            row = next(r for r in body["servers"] if r["server_id"] == srv["id"])
            assert (row["tools_called"], row["tools_known"]) == (1, 2)
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_a_retired_tool_does_not_inflate_the_used_ratio():
    """`tools_called/tools_known` is rendered as a ratio ("1/1 tools used"). If a
    tool is renamed inside the window and BOTH names see traffic, counting the
    historical one puts the numerator above the denominator — the dashboard would
    render an impossible "2/1"."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-ratio", auth="none")
        try:
            with Session(get_engine()) as session:
                repo.upsert_runtime(
                    session, srv["id"], state="running", tools=[{"name": "after"}]
                )
            recorder.record(srv["id"], ["before"])  # the pre-rename name
            recorder.record(srv["id"], ["after"])
            recorder.flush_sync()
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            row = next(r for r in body["servers"] if r["server_id"] == srv["id"])
            assert (row["tools_called"], row["tools_known"]) == (1, 1)
            # ...and the retired name is still listed, so the traffic isn't hidden.
            rows = {t["tool"]: t for t in body["tools"] if t["server_id"] == srv["id"]}
            assert rows["before"]["calls"] == 1 and rows["before"]["known"] is False
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_marks_a_tool_the_server_no_longer_exposes():
    """Traffic to a name that is no longer discovered (renamed, hidden, gone
    upstream) is still real — it stays listed, flagged."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-inst-retired", auth="none")
        try:
            with Session(get_engine()) as session:
                repo.upsert_runtime(session, srv["id"], state="running", tools=[{"name": "kept"}])
            recorder.record(srv["id"], ["gone"])
            recorder.flush_sync()
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            rows = {t["tool"]: t for t in body["tools"] if t["server_id"] == srv["id"]}
            assert rows["gone"]["calls"] == 1 and rows["gone"]["known"] is False
            assert rows["kept"]["calls"] == 0 and rows["kept"]["known"] is True
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_is_scoped_to_what_the_caller_can_see():
    """A member's totals sum over the servers they own — never a whole-instance
    number leaked through an aggregate."""
    from test_ownership import _mk, _reset, _setup

    _reset()
    try:
        with TestClient(app) as client:
            admin, member, _ = _setup(client)
            mine = _mk(client, member, "member-owned")
            theirs = _mk(client, admin, "admin-owned")
            recorder.record(mine["id"], ["search"])
            recorder.record(theirs["id"], ["secret_tool"])
            recorder.record(theirs["id"], ["secret_tool"])
            recorder.flush_sync()

            body = client.get("/api/usage?days=1", headers=member).json()
            assert body["tool_calls"] == 1
            assert [r["server_id"] for r in body["servers"]] == [mine["id"]]
            assert all(t["server_id"] == mine["id"] for t in body["tools"])
            assert sum(p["calls"] for p in body["series"]) == 1

            everything = client.get("/api/usage?days=1", headers=admin).json()
            assert everything["tool_calls"] == 3
            assert {r["server_id"] for r in everything["servers"]} == {mine["id"], theirs["id"]}
    finally:
        _reset()


def test_instance_usage_is_empty_but_valid_with_no_servers():
    with TestClient(app) as client:
        for existing in client.get("/api/servers", headers=LOOPBACK).json():
            client.delete(f"/api/servers/{existing['id']}", headers=LOOPBACK)
        body = client.get("/api/usage", headers=LOOPBACK).json()
        assert body["servers"] == [] and body["tools"] == []
        assert (body["tool_calls"], body["other_requests"], body["active_servers"]) == (0, 0, 0)
        assert len(body["series"]) == 7 and body["bucket_seconds"] == 86400


def test_instance_usage_splits_the_series_by_server():
    """The stacked view's bands line up index-for-index with the series, so the
    client can zip them without re-deriving buckets."""
    with TestClient(app) as client:
        a = create_server(client, name="usage-band-a", auth="none")
        b = create_server(client, name="usage-band-b", auth="none")
        try:
            recorder.record(a["id"], ["search"])
            recorder.record(a["id"], ["search"])
            recorder.record(b["id"], ["fetch"])
            recorder.record(b["id"])  # non-tool traffic: never in a band
            recorder.flush_sync()
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            bands = {band["slug"]: band for band in body["series_by_server"]}
            assert set(bands) == {a["slug"], b["slug"]}
            for band in bands.values():
                assert len(band["points"]) == len(body["series"])
            assert sum(bands[a["slug"]]["points"]) == 2
            assert sum(bands[b["slug"]]["points"]) == 1
            # every band summed equals the series' tool calls — nothing lost, nothing double
            assert sum(sum(b["points"]) for b in body["series_by_server"]) == body["tool_calls"]
        finally:
            for srv in (a, b):
                client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_folds_the_split_tail_into_one_other_band():
    """Past the cap the tail folds server-side, so the payload stays bounded and no
    client has to decide which servers to drop."""
    with TestClient(app) as client:
        servers = [
            create_server(client, name=f"usage-stack-{i}", auth="none")
            for i in range(stats.SPLIT_SERIES_LIMIT + 2)
        ]
        try:
            # Descending call counts, so the fold is deterministic: the two smallest
            # land in "Other".
            for rank, srv in enumerate(servers):
                for _ in range(len(servers) - rank):
                    recorder.record(srv["id"], ["tool"])
            recorder.flush_sync()
            body = client.get("/api/usage?days=1", headers=LOOPBACK).json()
            bands = body["series_by_server"]
            assert len(bands) == stats.SPLIT_SERIES_LIMIT + 1
            other = bands[-1]
            assert other["server_id"] is None and other["name"] == "Other"
            assert sum(other["points"]) == 1 + 2  # the two smallest servers
            assert sum(sum(b["points"]) for b in bands) == body["tool_calls"]
        finally:
            for srv in servers:
                client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_keeps_hourly_resolution_for_a_long_window():
    """The main series rolls up to days past 48h; the activity-by-hour view still
    needs the hour back (and the browser needs UTC to re-bucket it locally)."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-hourly", auth="none")
        try:
            now = datetime.now(timezone.utc)
            with Session(get_engine()) as session:
                repo.bump_usage(
                    session,
                    {
                        (srv["id"], "search", recorder.current_bucket(now)): 2,
                        (srv["id"], "search", recorder.current_bucket(now - timedelta(hours=5))): 1,
                        # non-tool traffic is not activity for this view
                        (srv["id"], usage.NOT_A_TOOL, recorder.current_bucket(now)): 4,
                    },
                )
            body = client.get("/api/usage?days=30", headers=LOOPBACK).json()
            assert body["bucket_seconds"] == 86400  # the main series rolled up...
            hourly = body["hourly"]
            assert len(hourly) == 2  # ...while this kept both hours, and dropped the quiet ones
            assert sum(h["calls"] for h in hourly) == 3
            assert all(h["bucket"].endswith("Z") or "+00:00" in h["bucket"] for h in hourly)
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_instance_usage_rejects_an_out_of_range_window():
    with TestClient(app) as client:
        for days in (0, usage.MAX_DAYS + 1):
            assert client.get(f"/api/usage?days={days}", headers=LOOPBACK).status_code == 422


# --- windowing / rollup ------------------------------------------------------- #


def test_long_windows_roll_up_to_daily_buckets():
    with Session(get_engine()) as session:
        result = stats.server_usage(session, "srv", days=30)
    assert result["bucket_seconds"] == 86400
    assert len(result["series"]) == 30


def test_series_excludes_buckets_older_than_the_window():
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        repo.bump_usage(
            session,
            {
                ("srv", "search", recorder.current_bucket(now)): 3,
                ("srv", "search", recorder.current_bucket(now - timedelta(days=9))): 5,
            },
        )
        result = stats.server_usage(session, "srv", days=7)
    assert result["tool_calls"] == 3  # the 9-day-old bucket is outside the window
    assert result["tools"][0]["calls"] == 3


# --- retention + cleanup ------------------------------------------------------ #


def test_prune_drops_buckets_past_retention():
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        repo.bump_usage(
            session,
            {
                ("srv", "search", recorder.current_bucket(now)): 1,
                ("srv", "search", recorder.current_bucket(now - timedelta(days=40))): 1,
            },
        )
        assert repo.prune_usage(session, now - timedelta(days=30)) == 1
        remaining = repo.usage_since(session, "srv", now - timedelta(days=90))
    assert len(remaining) == 1


def test_retention_setting_rejects_a_bad_value():
    with Session(get_engine()) as session:
        for bad in (-1, True, "30"):
            with pytest.raises(ValueError):
                runtime_settings.write(session, {"usage_retention_days": bad})


def test_retention_setting_is_bounded_above():
    """The prune builds `utcnow() - timedelta(days=value)`. A value large enough to
    underflow datetime would raise OverflowError there, breaking every flush — and
    the usage endpoints, which flush — until the setting was changed back."""
    with Session(get_engine()) as session:
        with pytest.raises(ValueError):
            runtime_settings.write(
                session,
                {"usage_retention_days": runtime_settings.MAX_USAGE_RETENTION_DAYS + 1},
            )
        try:
            runtime_settings.write(
                session, {"usage_retention_days": runtime_settings.MAX_USAGE_RETENTION_DAYS}
            )
            # the ceiling itself must be safe to actually prune with
            recorder.reset()
            recorder.flush_sync()
        finally:
            runtime_settings.write(session, {"usage_retention_days": 30})


def test_a_window_is_clamped_to_what_retention_kept():
    """Past the retention cutoff the buckets were deleted, so reporting them as
    dense zeroes would draw discarded history as genuine quiet."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-clamp", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"usage_retention_days": 5})
            body = client.get(f"/api/servers/{srv['id']}/usage?days=90", headers=LOOPBACK).json()
            assert len(body["series"]) == 5  # not 90
            since = datetime.fromisoformat(body["since"].replace("Z", "+00:00"))
            assert (datetime.now(timezone.utc) - since).days <= 5
            # 0 (keep forever) imposes no clamp
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"usage_retention_days": 0})
            body = client.get(f"/api/servers/{srv['id']}/usage?days=90", headers=LOOPBACK).json()
            assert len(body["series"]) == 90
        finally:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"usage_retention_days": 30})
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_retention_zero_keeps_everything():
    """0 is the operator's explicit opt-out, not 'delete immediately'."""
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        try:
            runtime_settings.write(session, {"usage_retention_days": 0})
            repo.bump_usage(session, {("srv", "search", recorder.current_bucket(now)): 1})
            recorder.reset()  # clear the prune clock so the next flush prunes
            recorder.flush_sync()
            assert len(repo.usage_since(session, "srv", now - timedelta(days=90))) == 1
        finally:
            runtime_settings.write(session, {"usage_retention_days": 30})


def test_counts_pending_at_delete_never_come_back_as_orphan_rows():
    """A flush lands up to an interval after the calls it counts. If a server is
    deleted in that window, neither the pending counts nor a request finishing
    mid-delete may write rows back — SQLite foreign keys are off, so nothing
    downstream would reject them and retention 0 would keep them forever."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-delete-race", auth="none")
        recorder.record(srv["id"], ["search"])  # pending, not yet flushed
        assert client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK).status_code == 204
        # The delete drops the pending counts...
        with recorder._lock:
            assert not [key for key in recorder._pending if key[0] == srv["id"]]
        # ...and a count that slipped in anyway (an in-flight request) is refused
        # by the write itself, which is the half that can't be raced.
        recorder.record(srv["id"], ["search"])
        recorder.flush_sync()
        assert _rows(srv["id"]) == {}


def test_deleting_a_server_drops_its_usage_rows():
    """A deleted server's counters must not outlive it as unreachable rows."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-deleted", auth="none")
        recorder.record(srv["id"], ["search"])
        recorder.flush_sync()
        assert _rows(srv["id"]) == {"search": 1}
        assert client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK).status_code == 204
        assert _rows(srv["id"]) == {}
