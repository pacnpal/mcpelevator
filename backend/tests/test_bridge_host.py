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
