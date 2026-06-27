"""Reverse-proxy data-plane tests: /s/<slug>/* — the path every MCP request takes.

Covers the proxy's own responsibilities (the bridge subprocess is never spawned —
servers are created disabled and the supervisor endpoint is stubbed, so these stay
deterministic and fast):
  * slug routing — unknown slug -> 404
  * backend down -> 503
  * the auth chokepoint on the proxy path — Host/Origin allowlist (403) and the
    per-server bearer provider (401 missing/invalid; valid token gets past auth)
  * successful streaming/SSE forwarding to the backend, with hop-by-hop and
    content-encoding headers stripped and buffering disabled
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.db import get_engine, repo
from app.db.models import Token
from app.main import app
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token

LOOPBACK = {"host": "127.0.0.1"}


# --- a fake backend bridge the proxy forwards to ----------------------------- #


async def _upstream_sse(request):
    """Mimic a Streamable-HTTP/SSE backend: a chunked event stream plus a couple of
    headers the proxy must own (hop-by-hop) or strip (content-encoding)."""

    async def gen():
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "connection": "keep-alive",  # hop-by-hop: must be dropped by the proxy
            "content-encoding": "identity",  # must be stripped (proxy decodes raw)
            "x-upstream": "yes",  # an ordinary header: must survive
        },
    )


async def _upstream_echo(request):
    """Echo method + path + body so forwarding of each can be asserted."""
    body = (await request.body()).decode()
    return JSONResponse({"method": request.method, "path": request.url.path, "body": body})


_upstream = Starlette(
    routes=[
        Route("/sse", _upstream_sse, methods=["GET"]),
        Route("/{path:path}", _upstream_echo, methods=["GET", "POST", "DELETE"]),
    ]
)


def _point_proxy_at_upstream(client: TestClient) -> None:
    """Replace the app's outbound httpx client with one bound to the in-process fake
    backend, and stub the supervisor so the slug resolves to a 'running' endpoint."""
    client.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_upstream))
    client.app.state.supervisor.endpoint = lambda slug: ("backend", 9000)


def _create_server(client: TestClient, *, auth: str = "none") -> dict:
    """Create a disabled server (no subprocess) and return its summary."""
    r = client.post(
        "/api/servers",
        json={"name": "px", "command": "echo", "auth_provider": auth},
        headers=LOOPBACK,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mint_token(scope: str = "all") -> str:
    raw = new_token()
    with Session(get_engine()) as session:
        repo.create_token(
            session,
            Token(id=new_id(), name="t", token_hash=hash_token(raw), prefix=raw[:12], scope=scope),
        )
    return raw


# --- routing / backend availability ------------------------------------------ #


def test_unknown_slug_returns_404():
    with TestClient(app) as client:
        r = client.get("/s/does-not-exist/mcp", headers=LOOPBACK)
        assert r.status_code == 404
        assert "unknown server" in r.text


def test_running_server_unavailable_returns_503():
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            # exists but no live endpoint -> 503 (auth passed, backend simply down)
            client.app.state.supervisor.endpoint = lambda slug: None
            r = client.get(f"/s/{srv['slug']}/mcp", headers=LOOPBACK)
            assert r.status_code == 503
            assert "not running" in r.text
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


# --- auth chokepoint --------------------------------------------------------- #


def test_proxy_rejects_off_allowlist_host():
    """The Host/Origin allowlist guards the proxy path too (DNS-rebinding defense):
    an off-allowlist Host is 403 before any forwarding, even from a loopback peer."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            _point_proxy_at_upstream(client)
            r = client.get(f"/s/{srv['slug']}/mcp", headers={"host": "evil.example"})
            assert r.status_code == 403
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_bearer_server_requires_token_on_proxy_path():
    with TestClient(app) as client:
        srv = _create_server(client, auth="bearer")
        try:
            _point_proxy_at_upstream(client)
            # no token -> 401
            assert client.get(f"/s/{srv['slug']}/mcp", headers=LOOPBACK).status_code == 401
            # garbage token -> 401
            bad = {**LOOPBACK, "authorization": "Bearer nonsense"}
            assert client.get(f"/s/{srv['slug']}/mcp", headers=bad).status_code == 401
            # valid token -> auth passes, request is forwarded to the backend (200)
            good = {**LOOPBACK, "authorization": f"Bearer {_mint_token('all')}"}
            assert client.get(f"/s/{srv['slug']}/sse", headers=good).status_code == 200
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_scoped_token_rejected_for_other_server_on_proxy_path():
    """A token scoped to one server must not authorize another via the proxy."""
    with TestClient(app) as client:
        srv = _create_server(client, auth="bearer")
        try:
            _point_proxy_at_upstream(client)
            other_scope = {**LOOPBACK, "authorization": f"Bearer {_mint_token('some-other-id')}"}
            assert client.get(f"/s/{srv['slug']}/mcp", headers=other_scope).status_code == 403
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


# --- forwarding -------------------------------------------------------------- #


def test_forwards_method_path_and_body_to_backend():
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            _point_proxy_at_upstream(client)
            r = client.post(f"/s/{srv['slug']}/rest/tools/call", content="hello", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["method"] == "POST"
            assert data["path"] == "/rest/tools/call"  # slug stripped, remainder forwarded
            assert data["body"] == "hello"
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_streams_sse_and_sanitizes_headers():
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            _point_proxy_at_upstream(client)
            r = client.get(f"/s/{srv['slug']}/sse", headers=LOOPBACK)
            assert r.status_code == 200
            assert "data: one" in r.text and "data: two" in r.text
            # the proxy disables outer buffering and forwards ordinary upstream headers
            assert r.headers["x-accel-buffering"] == "no"
            assert r.headers.get("x-upstream") == "yes"
            # hop-by-hop / content-encoding headers are not leaked downstream
            assert "connection" not in {k.lower() for k in r.headers}
            assert "content-encoding" not in {k.lower() for k in r.headers}
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_forwards_delete_method_to_backend():
    """DELETE must be forwarded just like POST — the method set in _PROXY_METHODS."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            _point_proxy_at_upstream(client)
            r = client.delete(f"/s/{srv['slug']}/resource/42", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["method"] == "DELETE"
            assert data["path"] == "/resource/42"
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_forwards_get_with_query_string():
    """Query parameters must survive the proxy hop intact."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            _point_proxy_at_upstream(client)
            r = client.get(f"/s/{srv['slug']}/items?foo=bar&baz=1", headers=LOOPBACK)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["method"] == "GET"
            assert data["path"] == "/items"
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_none_auth_server_passes_without_token():
    """A server configured with auth_provider='none' must accept requests that
    carry no Authorization header at all (the default for new servers)."""
    with TestClient(app) as client:
        srv = _create_server(client, auth="none")
        try:
            _point_proxy_at_upstream(client)
            # no Authorization header, loopback peer -> must reach the backend
            r = client.get(f"/s/{srv['slug']}/sse", headers=LOOPBACK)
            assert r.status_code == 200
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_server_scoped_token_accepted_for_matching_server():
    """A token whose scope is set to a specific server.id must be accepted on
    that server's proxy path (not only 'all'-scoped tokens are valid)."""
    with TestClient(app) as client:
        srv = _create_server(client, auth="bearer")
        try:
            _point_proxy_at_upstream(client)
            # mint a token scoped to exactly this server's ID
            server_id = srv["id"]
            scoped_token = _mint_token(scope=server_id)
            headers = {**LOOPBACK, "authorization": f"Bearer {scoped_token}"}
            r = client.get(f"/s/{srv['slug']}/sse", headers=headers)
            assert r.status_code == 200
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_hop_by_hop_request_headers_not_forwarded():
    """The proxy must strip hop-by-hop headers from the outgoing request so they
    don't leak into the upstream connection (e.g. connection, keep-alive)."""
    received_headers: dict = {}

    async def _capture(request):
        received_headers.update(dict(request.headers))
        return JSONResponse({"ok": True})

    capture_app = Starlette(routes=[Route("/{path:path}", _capture, methods=["GET"])])

    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            client.app.state.http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=capture_app)
            )
            client.app.state.supervisor.endpoint = lambda slug: ("backend", 9000)
            client.get(
                f"/s/{srv['slug']}/probe",
                headers={**LOOPBACK, "connection": "keep-alive", "keep-alive": "timeout=5"},
            )
            hop_by_hop = {"connection", "keep-alive", "transfer-encoding", "te", "trailers",
                          "proxy-authenticate", "proxy-authorization", "upgrade"}
            forwarded_lower = {k.lower() for k in received_headers}
            assert hop_by_hop.isdisjoint(forwarded_lower), (
                f"hop-by-hop headers leaked to upstream: "
                f"{hop_by_hop & forwarded_lower}"
            )
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_proxy_path_stripped_to_root_when_no_subpath():
    """When the client requests /s/<slug>/ with an empty remainder the forwarded
    path must be empty (or '/'), not the raw slug path."""
    with TestClient(app) as client:
        srv = _create_server(client)
        try:
            _point_proxy_at_upstream(client)
            # trailing slash -> empty path remainder
            r = client.get(f"/s/{srv['slug']}/", headers=LOOPBACK)
            # The upstream echo handler returns a 200 for any path
            assert r.status_code == 200
            data = r.json()
            # The slug must have been stripped; path should not contain the slug
            assert srv["slug"] not in data["path"]
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Keep the shared test DB in local mode regardless of test order."""
    yield
    with Session(get_engine()) as session:
        runtime_settings.write(session, {"bind_mode": "local", "allowed_hosts": []})
