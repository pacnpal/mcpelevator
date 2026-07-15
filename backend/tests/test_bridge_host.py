"""Bridge host tests — the per-server FastMCP proxy in front of one stdio server.

Focus is the roots-forwarding handler. An upstream stdio MCP server may ask its
client (the proxy) to list filesystem roots; FastMCP's default forwards that to
whichever client is connected over HTTP. Clients that don't support roots reject
the request, which the upstream server logs as a recurring
``MCP error -32603: received error listing roots``. ``_forward_roots`` degrades
to an empty list instead of surfacing that error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, sentinel

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from mcp.server.session import ServerSession
from mcp.types import Root

from app.bridge import host


def _ctx_with_session() -> MagicMock:
    """A Context whose .session is spec'd to the real ServerSession.

    Using ``spec=ServerSession`` means a typo'd or non-existent session method
    (e.g. accessing ``client_capabilities``, which the SDK does NOT expose)
    raises AttributeError instead of returning a truthy MagicMock — so these
    tests actually prove ``check_client_capability`` is a real SDK method.
    """
    ctx = MagicMock()
    ctx.session = MagicMock(spec=ServerSession)
    return ctx


@pytest.mark.asyncio
async def test_forward_roots_no_active_context_returns_empty():
    """No request context (e.g. asked during handshake) -> [] not an exception."""
    with patch.object(host, "get_context", side_effect=RuntimeError("No active context found.")):
        assert await host._forward_roots(None) == []


@pytest.mark.asyncio
async def test_forward_roots_skips_when_client_lacks_capability():
    """A client that never advertised roots is never asked — we just return []."""
    ctx = _ctx_with_session()
    ctx.session.check_client_capability.return_value = False
    ctx.list_roots = AsyncMock(side_effect=AssertionError("must not forward"))
    with patch.object(host, "get_context", return_value=ctx):
        assert await host._forward_roots(None) == []
    ctx.list_roots.assert_not_called()


@pytest.mark.asyncio
async def test_forward_roots_forwards_when_client_supports_it():
    """A capable client's roots are forwarded through unchanged."""
    roots = [Root(uri="file:///work")]
    ctx = _ctx_with_session()
    ctx.session.check_client_capability.return_value = True
    ctx.list_roots = AsyncMock(return_value=roots)
    with patch.object(host, "get_context", return_value=ctx):
        assert await host._forward_roots(None) == roots


@pytest.mark.asyncio
async def test_forward_roots_swallows_forwarding_errors():
    """Client claimed roots support but the call failed -> [], not -32603 upstream."""
    ctx = _ctx_with_session()
    ctx.session.check_client_capability.return_value = True
    ctx.list_roots = AsyncMock(side_effect=Exception("boom"))
    with patch.object(host, "get_context", return_value=ctx):
        assert await host._forward_roots(None) == []


def test_session_exposes_check_client_capability():
    """Lock in the SDK contract the handler relies on: ServerSession provides
    ``check_client_capability`` (and does NOT expose a ``client_capabilities``
    attribute). Guards against regressions if the mcp dependency changes."""
    assert hasattr(ServerSession, "check_client_capability")
    assert not hasattr(ServerSession, "client_capabilities")


def test_build_proxy_installs_custom_roots_handler():
    """build_proxy must wire _forward_roots onto the upstream ProxyClient and
    hand that client to create_proxy. Asserting the wiring (not just the return
    type) catches regressions that drop the custom handler or revert to the
    deprecated FastMCP.as_proxy path."""
    with (
        patch.object(host, "ProxyClient", autospec=True) as proxy_client_cls,
        patch.object(host, "create_proxy", return_value=sentinel.proxy) as create_proxy_mock,
    ):
        result = host.build_proxy({"command": "echo", "args": ["hi"], "name": "t"})

    assert result is sentinel.proxy
    assert proxy_client_cls.call_args.kwargs["roots"] is host._forward_roots
    create_proxy_mock.assert_called_once_with(proxy_client_cls.return_value, name="t")


@pytest.mark.asyncio
async def test_proxy_preserves_tool_output_schema():
    """Elevation must pass tool schemas through unchanged. A client connected to
    the bridge sees the upstream tool's ``outputSchema`` exactly as authored (and
    no schema invented for tools that don't declare one) — otherwise every client
    shows the "recommended: add an outputSchema" hint for tools that do have one.

    Uses an in-memory upstream (FastMCPTransport) in place of the stdio child so
    the whole round-trip runs without spawning processes.
    """

    answer_schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    upstream = FastMCP("upstream")

    @upstream.tool(output_schema=answer_schema)
    def structured(q: str):
        """Has an output schema."""
        return {"answer": q}

    @upstream.tool
    def unstructured(q: str):
        """No return annotation -> no output schema."""
        return q

    with patch.object(host, "_build_transport", return_value=FastMCPTransport(upstream)):
        proxy = host.build_proxy({"command": "ignored", "name": "t"})

    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}

    assert tools["structured"].outputSchema == answer_schema
    assert tools["unstructured"].outputSchema is None


def test_build_transport_stdio_is_default():
    """No transport / "stdio" → a StdioTransport from command/args (unchanged path)."""
    with (
        patch.dict(host.os.environ, {"PATH": "/bin", "HOME": "/tmp"}, clear=True),
        patch.object(host, "StdioTransport", autospec=True) as stdio,
    ):
        host._build_transport({"command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}})
    kwargs = stdio.call_args.kwargs
    assert kwargs["command"] == "npx"
    assert kwargs["args"] == ["-y", "pkg"]
    # server env is merged OVER the child's os.environ — both the server-specific key
    # and the inherited parent vars (PATH/HOME) must survive (the compatibility
    # guarantee npx/uvx rely on).
    assert kwargs["env"]["K"] == "v"
    assert kwargs["env"]["PATH"] == "/bin"
    assert kwargs["env"]["HOME"] == "/tmp"


def test_build_transport_streamable_http_uses_url_and_headers():
    """A remote streamable-http spec → StreamableHttpTransport(url, headers); the
    headers (spec env) must NOT be polluted with os.environ — they're HTTP headers."""
    spec = {
        "command": "https://up.example/mcp",
        "env": {"Authorization": "Bearer t"},
        "transport": "streamable-http",
    }
    with (
        patch.object(host, "StreamableHttpTransport", autospec=True) as shttp,
        patch.object(host, "StdioTransport", side_effect=AssertionError("must not spawn stdio")),
    ):
        host._build_transport(spec)
    kwargs = shttp.call_args.kwargs
    assert kwargs["url"] == "https://up.example/mcp"
    assert kwargs["headers"] == {"Authorization": "Bearer t"}  # exact, no os.environ


def test_build_transport_sse_uses_url_and_headers():
    spec = {"command": "https://up.example/sse", "env": {"X": "1"}, "transport": "sse"}
    with (
        patch.object(host, "SSETransport", autospec=True) as sse,
        patch.object(host, "StdioTransport", side_effect=AssertionError("must not spawn stdio")),
    ):
        host._build_transport(spec)
    kwargs = sse.call_args.kwargs
    assert kwargs["url"] == "https://up.example/sse"
    assert kwargs["headers"] == {"X": "1"}


def test_bridge_never_forwards_caller_headers_to_remote_upstream():
    """The outer proxy has already authenticated the caller, so a bridge must not
    pass that credential through to an operator-configured remote MCP server."""
    transport = host.StreamableHttpTransport(url="https://up.example/mcp")
    assert transport.forward_incoming_headers is False
    # ProxyClient/create_proxy turn forwarding on; build_proxy must override them last.
    with patch.object(host, "_build_transport", return_value=transport):
        host.build_proxy({"name": "remote"})
    assert transport.forward_incoming_headers is False
