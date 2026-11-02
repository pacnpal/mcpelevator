"""Shared pytest setup.

Pin ``MCPE_DATA_DIR`` to a throwaway directory BEFORE any ``app.*`` import caches
``get_settings()`` / ``get_engine()``, so a bare ``pytest`` run never reads or
writes a real data directory. An explicit ``MCPE_DATA_DIR`` still wins (setdefault).
"""

import os
import tempfile

os.environ.setdefault("MCPE_DATA_DIR", tempfile.mkdtemp(prefix="mcpe-tests-"))
# Tests count and spawn servers; the built-in one is provisioned explicitly where tested.
os.environ.setdefault("MCPE_CONTROL_MCP", "false")
# No background directory sync in tests; the mirror is exercised directly.
os.environ.setdefault("MCPE_CATALOG_MIRROR", "false")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402  (the env pin above must precede app imports)

from app.db import get_engine, init_db, repo  # noqa: E402
from app.db.models import Token  # noqa: E402
from app.util import hash_token, new_id, new_token  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db_schema():
    """Create the schema before any test runs, so tests that touch the DB WITHOUT
    starting the app (e.g. oauth_flow unit tests, whose promotion guard reads the
    committed server row) work in any subset/order. Normally app startup does this;
    a standalone run of one test file would otherwise hit "no such table". A fixture
    rather than a module-level call keeps collection side-effect-free; idempotent
    against the throwaway (or explicitly pinned) data dir."""
    init_db()

LOOPBACK = {"host": "127.0.0.1"}  # passes the Host allowlist (TestClient peer is loopback)


def mint_token(scope: str = "all") -> str:
    """Insert a token of the given scope into the shared DB; return the plaintext."""
    raw = new_token()
    with Session(get_engine()) as session:
        repo.create_token(
            session,
            Token(id=new_id(), name=scope, token_hash=hash_token(raw), prefix=raw[:12], scope=scope),
        )
    return raw


def create_server(client, *, name: str, auth: str | None = None) -> dict:
    """Create a disabled server (no subprocess) via POST /api/servers; return its summary."""
    payload: dict = {"name": name, "command": "echo"}
    if auth is not None:
        payload["auth_provider"] = auth
    r = client.post("/api/servers", json=payload, headers=LOOPBACK)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="session")
def pushing_upstream_url():
    """An MCP server, served over real Streamable HTTP, whose tools push requests to the
    client mid-call the handshake-era way: ``where`` asks for roots and returns them as
    JSON, ``roots_then_progress`` does the same and then reports progress (so the progress
comes after a round trip), the resource ``push://roots`` and the prompt ``roots_prompt``
return the roots too, ``slow_where`` waits ``delay`` seconds before it asks, and
``confirm`` elicits a name and returns ``"<action>:<name>"``. It's what a remote
    runner or a group member reaches over the network. (FastMCP's in-memory transport
    can't stand in: a handshake-era push over it deadlocks in-process.)"""
    import json
    import socket
    import threading
    import time

    import uvicorn
    from fastmcp import Context, FastMCP

    upstream = FastMCP("pushing")

    @upstream.tool
    async def where(ctx: Context) -> str:
        return json.dumps([str(r.uri) for r in (await ctx.session.list_roots()).roots])

    @upstream.tool
    async def roots_then_progress(ctx: Context) -> str:
        roots = (await ctx.session.list_roots()).roots
        await ctx.report_progress(1, 2)
        return json.dumps([str(r.uri) for r in roots])

    @upstream.resource("push://roots")
    async def roots_resource(ctx: Context) -> str:
        return json.dumps([str(r.uri) for r in (await ctx.session.list_roots()).roots])

    @upstream.prompt
    async def roots_prompt(ctx: Context) -> str:
        return json.dumps([str(r.uri) for r in (await ctx.session.list_roots()).roots])

    @upstream.tool
    async def slow_where(ctx: Context, delay: float) -> str:
        import asyncio

        await asyncio.sleep(delay)
        return json.dumps([str(r.uri) for r in (await ctx.session.list_roots()).roots])

    @upstream.tool
    async def confirm(ctx: Context) -> str:
        answer = await ctx.session.elicit(
            message="who are you?",
            requested_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
        return f"{answer.action}:{(answer.content or {}).get('name')}"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(upstream.http_app(path="/mcp"), host="127.0.0.1", port=port,
                       log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "upstream never started"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(5)
