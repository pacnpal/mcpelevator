"""Bridge host tests — the per-server FastMCP proxy in front of one stdio server.

Focus is the roots-forwarding handler. An upstream stdio MCP server may ask its
client (the proxy) to list filesystem roots; FastMCP's default forwards that to
whichever client is connected over HTTP. Clients that don't support roots reject
the request, which the upstream server logs as a recurring
``MCP error -32603: received error listing roots``. ``_forward_roots`` degrades
to an empty list instead of surfacing that error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from mcp.types import Root

from app.bridge import host


@pytest.mark.asyncio
async def test_forward_roots_no_active_context_returns_empty():
    """No request context (e.g. asked during handshake) -> [] not an exception."""
    with patch.object(host, "get_context", side_effect=RuntimeError("No active context found.")):
        assert await host._forward_roots(None) == []


@pytest.mark.asyncio
async def test_forward_roots_skips_when_client_lacks_capability():
    """A client that never advertised roots is never asked — we just return []."""
    ctx = MagicMock()
    ctx.session.check_client_capability.return_value = False
    ctx.list_roots = AsyncMock(side_effect=AssertionError("must not forward"))
    with patch.object(host, "get_context", return_value=ctx):
        assert await host._forward_roots(None) == []
    ctx.list_roots.assert_not_called()


@pytest.mark.asyncio
async def test_forward_roots_forwards_when_client_supports_it():
    """A capable client's roots are forwarded through unchanged."""
    roots = [Root(uri="file:///work")]
    ctx = MagicMock()
    ctx.session.check_client_capability.return_value = True
    ctx.list_roots = AsyncMock(return_value=roots)
    with patch.object(host, "get_context", return_value=ctx):
        assert await host._forward_roots(None) == roots


@pytest.mark.asyncio
async def test_forward_roots_swallows_forwarding_errors():
    """Client claimed roots support but the call failed -> [], not -32603 upstream."""
    ctx = MagicMock()
    ctx.session.check_client_capability.return_value = True
    ctx.list_roots = AsyncMock(side_effect=Exception("boom"))
    with patch.object(host, "get_context", return_value=ctx):
        assert await host._forward_roots(None) == []


def test_build_proxy_installs_custom_roots_handler():
    """build_proxy wires our tolerant handler onto the upstream ProxyClient
    instead of FastMCP's default forwarder."""
    proxy = host.build_proxy({"command": "echo", "args": ["hi"], "name": "t"})
    assert isinstance(proxy, FastMCP)
