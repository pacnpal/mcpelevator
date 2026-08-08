"""Bridge host tests — the per-server FastMCP proxy in front of one stdio server.

Focus is the roots-forwarding handler. An upstream stdio MCP server may ask its
client (the proxy) to list filesystem roots; FastMCP's default forwards that to
whichever client is connected over HTTP. Clients that don't support roots reject
the request, which the upstream server logs as a recurring
``MCP error -32603: received error listing roots``. ``_forward_roots`` degrades
to an empty list instead of surfacing that error.
"""

from __future__ import annotations

import json
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


# --- per-tool policy: hiding (issue #105) + overrides (issue #112) ------------
#
# Both ride ONE FastMCP transform built by host._tool_transform, so these tests assert
# the behaviour clients see rather than the mechanism that produces it.


def _upstream_three_tools() -> FastMCP:
    upstream = FastMCP("upstream")

    @upstream.tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @upstream.tool
    def secret() -> str:
        """Internal-only; should be hideable."""
        return "classified"

    @upstream.tool
    def echo(s: str) -> str:
        """Echo the input."""
        return s

    return upstream


def _proxy_with(**policy) -> FastMCP:
    """A bridge proxy over the three-tool upstream, carrying a per-tool policy
    (``disabled_tools`` and/or ``tool_overrides``)."""
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_three_tools())
    ):
        return host.build_proxy({"command": "ignored", "name": "t", **policy})


def _proxy_with_disabled(disabled: list[str]) -> FastMCP:
    return _proxy_with(disabled_tools=disabled)


@pytest.mark.asyncio
async def test_disabled_tool_hidden_from_list():
    """A disabled tool must not appear in tools/list on the bridge surface."""
    proxy = _proxy_with_disabled(["secret"])
    async with Client(proxy) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"add", "echo"}  # secret filtered out


@pytest.mark.asyncio
async def test_disabled_tool_refused_on_call():
    """Hiding also disables: calling a disabled tool errors like an unknown one, so a
    client holding a stale list can't still invoke it."""
    proxy = _proxy_with_disabled(["secret"])
    async with Client(proxy) as client:
        with pytest.raises(Exception):  # noqa: PT011 — client surfaces a ToolError/McpError
            await client.call_tool("secret", {})
        # A non-disabled tool still works end to end.
        result = await client.call_tool("add", {"a": 2, "b": 3})
    assert result.data == 5


@pytest.mark.asyncio
async def test_no_disabled_tools_installs_no_filter():
    """Empty list = expose everything (the default); no middleware attached."""
    proxy = _proxy_with_disabled([])
    async with Client(proxy) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"add", "secret", "echo"}


def test_build_proxy_skips_transform_when_no_tool_policy():
    """The transform is only added when there's something to apply, so an unmodified
    server pays nothing. Asserted against the sentinel proxy so add_transform would
    raise if it were called."""
    with (
        patch.object(host, "ProxyClient", autospec=True),
        patch.object(host, "create_proxy", return_value=sentinel.proxy),
    ):
        # sentinel.proxy has no add_transform — a call would AttributeError.
        assert host.build_proxy({"command": "echo", "name": "t"}) is sentinel.proxy
    # An override map whose entries are all empty is likewise nothing to apply.
    assert host._tool_transform({"tool_overrides": {"add": {}}}) is None


def test_build_proxy_installs_transform_when_tool_policy_present():
    for policy in ({"disabled_tools": ["secret"]}, {"tool_overrides": {"add": {"name": "sum"}}}):
        proxy = MagicMock()
        with (
            patch.object(host, "ProxyClient", autospec=True),
            patch.object(host, "create_proxy", return_value=proxy),
        ):
            host.build_proxy({"command": "echo", "name": "t", **policy})
        assert proxy.add_transform.call_count == 1
        assert isinstance(proxy.add_transform.call_args.args[0], host.ToolTransform)


@pytest.mark.asyncio
async def test_tool_override_renames_and_redescribes():
    """The operator's replacement name/description is what clients discover — and the
    tool is callable under the new name, with the upstream's behaviour intact."""
    proxy = _proxy_with(
        tool_overrides={"add": {"name": "sum_numbers", "description": "Adds two numbers."}}
    )
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"sum_numbers", "secret", "echo"}
        assert tools["sum_numbers"].description == "Adds two numbers."
        # The input schema still comes from upstream — only the labels change.
        assert set(tools["sum_numbers"].inputSchema["properties"]) == {"a", "b"}
        result = await client.call_tool("sum_numbers", {"a": 2, "b": 3})
    assert result.data == 5


@pytest.mark.asyncio
async def test_tool_override_fields_are_independent():
    """Overriding only the description keeps the upstream name (and vice versa), so an
    operator can fix one without restating the other."""
    proxy = _proxy_with(
        tool_overrides={"add": {"description": "Better."}, "echo": {"name": "repeat"}}
    )
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["add"].description == "Better."  # renamed nothing
    assert tools["repeat"].description == "Echo the input."  # kept upstream's description


@pytest.mark.asyncio
async def test_renamed_tool_stops_answering_to_its_upstream_name():
    """A rename replaces the name rather than aliasing it — the upstream name is gone
    from every surface, exactly as if the server itself had been rebuilt."""
    proxy = _proxy_with(tool_overrides={"add": {"name": "sum_numbers"}})
    async with Client(proxy) as client:
        with pytest.raises(Exception, match="add"):  # noqa: PT011 — ToolError/McpError
            await client.call_tool("add", {"a": 1, "b": 2})


@pytest.mark.asyncio
async def test_hiding_wins_over_renaming():
    """A tool that's both disabled and renamed is simply gone — under either name — so
    the two controls can't combine into an exposed tool."""
    proxy = _proxy_with(
        disabled_tools=["secret"], tool_overrides={"secret": {"name": "internal"}}
    )
    async with Client(proxy) as client:
        names = {t.name for t in await client.list_tools()}
        assert names == {"add", "echo"}
        for name in ("secret", "internal"):
            with pytest.raises(Exception):  # noqa: PT011 — ToolError/McpError
                await client.call_tool(name, {})


@pytest.mark.asyncio
async def test_override_for_unknown_tool_is_a_no_op():
    """A stale key — a tool the upstream no longer exposes — must not break the bridge;
    the operator's other tools keep working while they clean it up."""
    proxy = _proxy_with(tool_overrides={"ghost": {"name": "casper"}})
    async with Client(proxy) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"add", "secret", "echo"}


@pytest.mark.asyncio
async def test_overrides_reach_the_rest_surface():
    """The REST surface generates its routes and OpenAPI from the same tool list, so a
    renamed tool is served at its NEW path — one policy, every surface."""
    proxy = _proxy_with(
        disabled_tools=["secret"], tool_overrides={"add": {"name": "sum_numbers"}}
    )
    routes = host.build_rest_routes(proxy, {"name": "t"})
    openapi = next(r for r in routes if r.path == "/rest/openapi.json")
    response = await openapi.endpoint(MagicMock())
    document = json.loads(response.body)
    assert set(document["paths"]) == {"/rest/sum_numbers", "/rest/echo"}
