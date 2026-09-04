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
from app.db.models import UsageBucket
from app.main import app
from app.registry import settings as runtime_settings
from app.usage import attribution, recorder, stats


# --- fixtures / helpers ------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_usage():
    """Counters are process-global; start and finish every test with none pending
    and no stored rows, so ordering can't leak counts between tests."""
    recorder.reset()
    _clear_rows()
    yield
    recorder.reset()
    _clear_rows()


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


def test_proxy_tools_reads_mcp_body_and_rest_path():
    body = json.dumps(_call("search")).encode()
    assert attribution.proxy_tools("POST", "mcp", body) == ["search"]
    assert attribution.proxy_tools("POST", "/mcp/", body) == ["search"]
    # the REST mirror names the tool in the path instead
    assert attribution.proxy_tools("POST", "rest/echo", b"{}") == ["echo"]


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


def _stub_group(client: TestClient, name: str, inner) -> None:
    client.app.state.supervisor.on_converged = None
    hub = client.app.state.groups
    prev = hub.app_for
    hub.app_for = lambda n, _prev=prev: inner if n == name else _prev(n)


def test_group_call_counts_against_the_member_that_owns_the_tool():
    """A tool reached through a bundle is the same tool call a direct /s request
    would make, so it lands in the same per-server counters."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _echo_body_inner)
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


def test_group_non_tool_traffic_is_not_charged_to_members():
    """initialize/tools/list fan out to every member; charging each would invent
    traffic none of them individually received."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-group-list", auth="none")
        try:
            with Session(get_engine()) as session:
                runtime_settings.write(session, {"groups": {"team": [srv["id"]]}})
            _stub_group(client, "team", _echo_body_inner)
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


def test_deleting_a_server_drops_its_usage_rows():
    """A deleted server's counters must not outlive it as unreachable rows."""
    with TestClient(app) as client:
        srv = create_server(client, name="usage-deleted", auth="none")
        recorder.record(srv["id"], ["search"])
        recorder.flush_sync()
        assert _rows(srv["id"]) == {"search": 1}
        assert client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK).status_code == 204
        assert _rows(srv["id"]) == {}
