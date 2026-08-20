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
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import Tool as FastMCPTool
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
    proxy = MagicMock()
    with (
        patch.object(host, "ProxyClient", autospec=True) as proxy_client_cls,
        patch.object(host, "create_proxy", return_value=proxy) as create_proxy_mock,
    ):
        result = host.build_proxy({"command": "echo", "args": ["hi"], "name": "t"})

    assert result is proxy
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
        with pytest.raises(ToolError, match="Unknown tool"):
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


def test_build_proxy_always_installs_the_transform():
    """Installed even with no policy: it also strips our reserved identity key from
    upstream tools, and a server can forge that key whether or not a policy is set."""
    proxy = MagicMock()
    with (
        patch.object(host, "ProxyClient", autospec=True),
        patch.object(host, "create_proxy", return_value=proxy),
    ):
        host.build_proxy({"command": "echo", "name": "t"})
    assert proxy.add_transform.call_count == 1
    assert isinstance(proxy.add_transform.call_args.args[0], host.ToolTransform)
    # An override map whose entries are all empty applies nothing.
    assert host._tool_transform({"tool_overrides": {"add": {}}})._transforms == {}


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
        with pytest.raises(ToolError, match="Unknown tool"):
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
            with pytest.raises(ToolError, match="Unknown tool"):
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


@pytest.mark.asyncio
async def test_renamed_tool_carries_its_upstream_name_in_meta():
    """A rename must not cost the tool its identity. The bridge stamps the upstream name
    into `_meta`, which the control plane's probe caches (supervisor.unit.tool_summary) so
    the UI can key its per-tool rows off something stable. Inferring identity by reversing
    the rename map instead would misidentify a tool whenever an exposed name isn't unique
    (a stale override key whose target later matches a real upstream tool)."""
    proxy = _proxy_with(tool_overrides={"add": {"name": "sum_numbers"}})
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}

    assert tools["sum_numbers"].meta[host.UPSTREAM_META_KEY] == {"name": "add"}
    # A tool that ISN'T renamed carries no such marker — its name already is the upstream
    # name, and stamping every tool would bloat every listing.
    assert host.UPSTREAM_META_KEY not in (tools["echo"].meta or {})


@pytest.mark.asyncio
async def test_description_only_override_leaves_identity_alone():
    """Only a rename needs the identity marker; re-describing doesn't move the name."""
    proxy = _proxy_with(tool_overrides={"add": {"description": "Better."}})
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert host.UPSTREAM_META_KEY not in (tools["add"].meta or {})


# --- fidelity: an override changes the LABELS, nothing else ---------------------


def _tool_with(**fields):
    """An upstream tool carrying fields FastMCP's transform is known to drop."""

    def fn(x: int) -> int:
        """Upstream tool."""
        return x

    return FastMCPTool.from_function(fn, name="tasky").model_copy(update=fields)


def _upstream_with_tool(tool) -> FastMCP:
    upstream = FastMCP("upstream")
    upstream.add_tool(tool)
    return upstream


@pytest.mark.asyncio
async def test_override_preserves_upstream_meta_and_execution():
    """Relabelling must not silently rewrite the rest of the tool definition.

    Two things FastMCP's own transform drops: ``ToolTransformConfig.meta`` REPLACES the
    upstream's `_meta` (so our identity marker would evict vendor extensions and client
    presentation hints), and ``from_tool`` doesn't copy ``execution`` (so a tool declaring
    MCP task support would lose ``taskSupport`` — and clients would then make an ordinary
    call it rejects — through even a description-only edit)."""
    tool = _tool_with(meta={"vendor": {"hint": "keep-me"}}, execution={"taskSupport": "required"})

    for policy in ({"description": "Desc only."}, {"name": "renamed"}):
        with patch.object(
            host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
        ):
            proxy = host.build_proxy(
                {"command": "x", "name": "t", "tool_overrides": {"tasky": policy}}
            )
        async with Client(proxy) as client:
            served = (await client.list_tools())[0]

        assert served.meta["vendor"] == {"hint": "keep-me"}, policy
        assert served.execution.taskSupport == "required", policy


@pytest.mark.asyncio
async def test_stale_rename_does_not_strand_a_real_tool_of_that_name():
    """A leftover override for a tool the upstream dropped must not reserve its target
    name. FastMCP reverse-maps a call on that name to the vanished source and answers
    "Unknown tool" — while tools/list still advertises the REAL tool of that name, which
    is then listed but uncallable. The name belongs to whoever actually carries it."""
    proxy = _proxy_with(tool_overrides={"ghost": {"name": "echo"}})
    async with Client(proxy) as client:
        assert {t.name for t in await client.list_tools()} == {"add", "secret", "echo"}
        assert (await client.call_tool("echo", {"s": "hi"})).data == "hi"


@pytest.mark.asyncio
async def test_override_preserves_upstream_icons():
    """`icons` is one more field FastMCP's transform nulls out (see _CARRIED_FIELDS) —
    an operator rewording a description must not strip the tool's icon."""
    tool = _tool_with(icons=[{"src": "https://example.test/i.png", "mimeType": "image/png"}])
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy(
            {"command": "x", "name": "t", "tool_overrides": {"tasky": {"description": "D."}}}
        )
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert str(served.icons[0].src) == "https://example.test/i.png"


@pytest.mark.asyncio
async def test_rename_onto_a_live_tool_goes_inert_instead_of_shadowing():
    """A rename must never take a name another live tool already answers to. Applying it
    would misroute BOTH tools — calls to the target would reach the renamed tool, and the
    native one would become unreachable. The UI can't be the only guard: it compares
    against the tool list as it was BEFORE the change, so an override written straight to
    the API (or an upstream that later adds a tool of that name) lands here anyway."""
    proxy = _proxy_with(tool_overrides={"add": {"name": "echo"}})
    async with Client(proxy) as client:
        assert {t.name for t in await client.list_tools()} == {"add", "secret", "echo"}
        # Each name still reaches the tool that actually owns it.
        assert (await client.call_tool("echo", {"s": "hi"})).data == "hi"
        assert (await client.call_tool("add", {"a": 1, "b": 2})).data == 3


@pytest.mark.asyncio
async def test_upstream_cannot_forge_the_identity_marker():
    """UPSTREAM_META_KEY asserts "the elevator renamed this tool". An upstream that sets it
    would hand the UI a false identity to key per-tool policy off — the operator would
    disable one row while a different tool stayed exposed. It's stripped from every upstream
    tool, so only this bridge can put it there."""
    tool = _tool_with(meta={"vendor": "keep", host.UPSTREAM_META_KEY: {"name": "impersonated"}})
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy(
            {"command": "x", "name": "t", "tool_overrides": {"tasky": {"description": "D."}}}
        )
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert host.UPSTREAM_META_KEY not in served.meta
    assert served.meta["vendor"] == "keep"  # the rest of the upstream's meta is untouched


@pytest.mark.asyncio
async def test_hiding_a_tool_frees_its_name_for_a_rename():
    """A hidden tool exposes no name, so another tool may take it. FastMCP's own transform
    reserves a target for EVERY entry and raises on a duplicate — which made this
    combination (accepted by both the UI and the API) a ValueError at proxy build, i.e. a
    bridge that crash-loops and takes the server offline."""
    proxy = _proxy_with(disabled_tools=["echo"], tool_overrides={"add": {"name": "echo"}})
    async with Client(proxy) as client:
        assert {t.name for t in await client.list_tools()} == {"echo", "secret"}
        assert (await client.call_tool("echo", {"a": 1, "b": 2})).data == 3  # renamed add
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("add", {"a": 1, "b": 2})


@pytest.mark.asyncio
async def test_a_labels_only_key_does_not_swallow_a_rename_onto_its_name():
    """A rename onto a name held only by a STALE override key must still resolve.

    Both halves of this policy are accepted by the write path: `add` -> `zz` is a rename
    onto a free name, and the `zz` entry is a description for a tool that isn't there right
    now (keys are allowed to go stale). The two entries meet in the reverse map, where an
    un-renamed key used to claim its own name — and since entries arrive sorted, `zz`
    overwrote the mapping `zz` -> `add` written a moment earlier. tools/list still
    advertised the renamed `zz` while a call on it went looking for a native `zz`, leaving
    the tool listed and uncallable."""
    proxy = _proxy_with(tool_overrides={"add": {"name": "zz"}, "zz": {"description": "stale"}})
    async with Client(proxy) as client:
        assert {t.name for t in await client.list_tools()} == {"zz", "secret", "echo"}
        assert (await client.call_tool("zz", {"a": 1, "b": 2})).data == 3


@pytest.mark.asyncio
async def test_refusing_a_rename_does_not_unhide_the_tool():
    """Hiding wins over renaming — including when the rename is refused because its target
    is taken. Dropping the whole config in that branch would expose a tool the operator
    disabled."""
    proxy = _proxy_with(disabled_tools=["add"], tool_overrides={"add": {"name": "echo"}})
    async with Client(proxy) as client:
        names = {t.name for t in await client.list_tools()}
        assert "add" not in names and "echo" in names
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("add", {"a": 1, "b": 2})
        assert (await client.call_tool("echo", {"s": "hi"})).data == "hi"  # native echo


@pytest.mark.asyncio
async def test_identity_marker_is_scrubbed_without_any_policy():
    """The scrub can't be conditional on a policy: an upstream can forge the reserved key
    whether or not the operator has configured anything for that server."""
    tool = _tool_with(meta={"keep": 1, host.UPSTREAM_META_KEY: {"name": "forged"}})
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t"})  # no overrides at all
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert host.UPSTREAM_META_KEY not in served.meta
    assert served.meta["keep"] == 1


@pytest.mark.asyncio
async def test_stale_rename_onto_a_hidden_name_does_not_leak_it():
    """A stale rename whose target is a HIDDEN tool must not hand that tool back. The name
    reverts to whoever carries it — under that tool's own policy — so "hidden tools are
    refused on call" still holds for a client that guesses the name."""
    proxy = _proxy_with(
        disabled_tools=["secret"], tool_overrides={"ghost": {"name": "secret"}}
    )
    async with Client(proxy) as client:
        assert {t.name for t in await client.list_tools()} == {"add", "echo"}
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("secret", {})


@pytest.mark.asyncio
async def test_override_preserves_an_open_ended_input_schema():
    """The transform REBUILDS the input schema, and the rebuild isn't faithful: an
    open-ended `additionalProperties: true` comes back as empty `properties` with
    `additionalProperties: false`, so a tool taking dynamic keys becomes uncallable with
    its real arguments after a description-only edit. The operator changed the labels; the
    input contract belongs to the upstream."""
    open_ended = {"type": "object", "additionalProperties": True}
    tool = _tool_with(parameters=open_ended)

    for policy in ({"description": "D."}, {"name": "renamed"}):
        with patch.object(
            host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
        ):
            proxy = host.build_proxy(
                {"command": "x", "name": "t", "tool_overrides": {"tasky": policy}}
            )
        async with Client(proxy) as client:
            served = (await client.list_tools())[0]
        assert served.inputSchema == open_ended, policy


@pytest.mark.asyncio
async def test_override_keeps_a_dynamic_schema_tool_callable():
    """The advertised schema surviving isn't enough — the tool has to still WORK. FastMCP's
    transform derives its forwarding closure from the schema it rebuilds, so a tool with an
    open-ended schema rejected every argument it was handed (valid ones included) after a
    description-only edit. Relabelling copies the source tool instead of rebuilding it, so
    dispatch stays the upstream's."""
    upstream = FastMCP("upstream")

    def dyn(a: int) -> dict:
        """Dynamic."""
        return {"a": a}

    upstream.add_tool(
        FastMCPTool.from_function(dyn, name="dyn").model_copy(
            update={"parameters": {"type": "object", "additionalProperties": True}}
        )
    )

    for policy, exposed in (({"description": "D."}, "dyn"), ({"name": "renamed"}, "renamed")):
        with patch.object(host, "_build_transport", return_value=FastMCPTransport(upstream)):
            proxy = host.build_proxy(
                {"command": "x", "name": "t", "tool_overrides": {"dyn": policy}}
            )
        async with Client(proxy) as client:
            result = await client.call_tool(exposed, {"a": 7}, raise_on_error=False)
        assert result.is_error is False, policy
        assert result.structured_content == {"a": 7}, policy


# --- schema dialect normalization (issue #123) -----------------------------------
#
# The MCP TypeScript SDK hardcodes a draft-07 `$schema` into every generated tool
# schema with no config option; a strict client whose validator only supports
# 2020-12 refuses every such tool outright. `normalize_schema_dialect` rewrites just
# the dialect pointer so the schema's actual keywords (which the upstream server, not
# this bridge, is responsible for) are left untouched.

_DRAFT_07 = "http://json-schema.org/draft-07/schema#"
_DIALECT_2020_12 = host._SCHEMA_DIALECT_2020_12


def _tool_with_schemas(*, parameters=None, output_schema=None):
    def fn(x: int) -> int:
        """Upstream tool."""
        return x

    update = {}
    if parameters is not None:
        update["parameters"] = parameters
    if output_schema is not None:
        update["output_schema"] = output_schema
    return FastMCPTool.from_function(fn, name="tasky").model_copy(update=update)


@pytest.mark.asyncio
async def test_normalize_schema_dialect_off_by_default_leaves_draft07_untouched():
    """Opt-in: without the flag, a draft-07 dialect passes through unchanged — this
    bridge must not silently rewrite what the upstream literally declared."""
    tool = _tool_with_schemas(
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}, "$schema": _DRAFT_07},
        output_schema={"type": "integer", "$schema": _DRAFT_07},
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t"})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DRAFT_07
    assert served.outputSchema["$schema"] == _DRAFT_07


@pytest.mark.asyncio
async def test_normalize_schema_dialect_rewrites_input_and_output_schema():
    """Enabled: a legacy dialect on EITHER schema is rewritten to 2020-12, and nothing
    else about the schema (its actual keywords) moves."""
    tool = _tool_with_schemas(
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}, "$schema": _DRAFT_07},
        output_schema={"type": "integer", "$schema": _DRAFT_07},
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DIALECT_2020_12
    assert served.inputSchema["properties"] == {"x": {"type": "integer"}}
    assert served.outputSchema["$schema"] == _DIALECT_2020_12
    assert served.outputSchema["type"] == "integer"


@pytest.mark.asyncio
async def test_normalize_schema_dialect_does_not_invent_a_dialect():
    """A schema declaring no `$schema` at all must not have one injected — normalizing
    is a rewrite of an existing (wrong) dialect, not an assertion of a new one."""
    tool = _tool_with_schemas(parameters={"type": "object", "properties": {}})
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert "$schema" not in served.inputSchema


@pytest.mark.asyncio
async def test_normalize_schema_dialect_leaves_2020_12_alone():
    """Already the target dialect: rewritten to the same value, i.e. a no-op."""
    tool = _tool_with_schemas(
        parameters={"type": "object", "properties": {}, "$schema": _DIALECT_2020_12}
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DIALECT_2020_12


@pytest.mark.asyncio
async def test_normalize_schema_dialect_applies_without_any_hide_or_rename_policy():
    """The dialect rewrite is orthogonal to disabled_tools/tool_overrides — it must fire
    on a tool that carries neither, not only on tools an operator has otherwise touched."""
    tool = _tool_with_schemas(
        parameters={"type": "object", "properties": {}, "$schema": _DRAFT_07}
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DIALECT_2020_12


@pytest.mark.asyncio
async def test_normalize_schema_dialect_combines_with_rename():
    """A renamed tool still gets its dialect rewritten — the two controls compose."""
    tool = _tool_with_schemas(
        parameters={"type": "object", "properties": {}, "$schema": _DRAFT_07}
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy(
            {
                "command": "x",
                "name": "t",
                "normalize_schema_dialect": True,
                "tool_overrides": {"tasky": {"name": "renamed"}},
            }
        )
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.name == "renamed"
    assert served.inputSchema["$schema"] == _DIALECT_2020_12


def test_tool_transform_reads_normalize_schema_dialect_from_spec():
    """Unit-level: the spec key reaches the transform's constructor flag."""
    assert host._tool_transform({})._normalize_schema_dialect is False
    assert host._tool_transform({"normalize_schema_dialect": True})._normalize_schema_dialect is True


# --- schema dialect normalization: don't mis-declare an incompatible construct ---
#
# Some draft-07 keywords change MEANING under 2020-12 rather than just being renamed.
# Relabeling `$schema` alone on a schema using one of them would silently invalidate or
# drop a constraint rather than merely fix the dialect pointer (review on PR #124).


@pytest.mark.asyncio
async def test_normalize_schema_dialect_skips_tuple_typed_items():
    """An array-valued `items` is draft-07 positional TUPLE validation — not a valid
    `items` shape in 2020-12 (tuples moved to `prefixItems`). Left as draft-07 rather
    than mislabeled as 2020-12 while still speaking draft-07's tuple syntax."""
    tool = _tool_with_schemas(
        parameters={
            "type": "array",
            "items": [{"type": "string"}, {"type": "integer"}],
            "$schema": _DRAFT_07,
        }
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DRAFT_07
    assert served.inputSchema["items"] == [{"type": "string"}, {"type": "integer"}]


@pytest.mark.asyncio
async def test_normalize_schema_dialect_skips_dependencies_keyword():
    """`dependencies` has no 2020-12 keyword of that name (split into
    `dependentRequired`/`dependentSchemas`) — a 2020-12 validator would just ignore it,
    silently dropping the constraint. Left under draft-07 instead."""
    tool = _tool_with_schemas(
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "dependencies": {"a": ["b"]},
            "$schema": _DRAFT_07,
        }
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DRAFT_07
    assert served.inputSchema["dependencies"] == {"a": ["b"]}


@pytest.mark.asyncio
async def test_normalize_schema_dialect_skips_ref_with_assertion_sibling():
    """draft-07 ignores every sibling of `$ref`; 2020-12 evaluates them. Relabeling would
    make the schema STRICTER — an argument the upstream tool previously accepted (the
    sibling `required` was silently ignored) could start failing validation post-proxy."""
    tool = _tool_with_schemas(
        parameters={"$ref": "#/$defs/Thing", "required": ["name"], "$schema": _DRAFT_07}
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DRAFT_07
    assert served.inputSchema["required"] == ["name"]


@pytest.mark.asyncio
async def test_normalize_schema_dialect_skips_nested_tuple_items():
    """The incompatible construct can be buried inside a property's own schema, not
    just at the top level — checked recursively so a top-level-clean schema with a
    nested tuple isn't waved through."""
    tool = _tool_with_schemas(
        parameters={
            "type": "object",
            "properties": {
                "pair": {"type": "array", "items": [{"type": "string"}, {"type": "string"}]}
            },
            "$schema": _DRAFT_07,
        }
    )
    with patch.object(
        host, "_build_transport", return_value=FastMCPTransport(_upstream_with_tool(tool))
    ):
        proxy = host.build_proxy({"command": "x", "name": "t", "normalize_schema_dialect": True})
    async with Client(proxy) as client:
        served = (await client.list_tools())[0]
    assert served.inputSchema["$schema"] == _DRAFT_07


# (schema, expected, id) — one row per behaviour, so a regression names the exact case and
# the remaining cases still run.
_INCOMPATIBLE_CONSTRUCT_CASES = [
    ({"items": [{"type": "string"}]}, True, "tuple-form-items"),
    ({"dependencies": {"a": ["b"]}}, True, "dependencies"),
    # A single-schema `items` (the ordinary, dialect-portable list-validation shape) is fine.
    ({"items": {"type": "string"}}, False, "single-schema-items"),
    ({"type": "object", "properties": {}}, False, "plain-object"),
    ("not a schema", False, "not-a-dict"),
    # A PROPERTY NAMED "dependencies" is not the `dependencies` KEYWORD — `properties`' keys
    # are arbitrary names, never schema keywords (review on #124: a naive "recurse into every
    # dict value" walk mistook this for the incompatible construct).
    (
        {"type": "object", "properties": {"dependencies": {"type": "string"}}},
        False,
        "property-named-dependencies",
    ),
    # Same for a property literally named "items" holding an array-valued schema keyword one
    # level down inside ITS OWN `items` — a real nested tuple, correctly still caught.
    (
        {"properties": {"items": {"type": "array", "items": [{"type": "string"}]}}},
        True,
        "real-tuple-under-property-named-items",
    ),
    # Genuinely nested through a real schema position (`items`, a single sub-schema).
    ({"type": "array", "items": {"dependencies": {}}}, True, "nested-dependencies"),
    # `$ref` next to an ASSERTION sibling (`required`): draft-07 ignores the sibling and
    # validates only the reference; 2020-12 evaluates both — relabeling would make the schema
    # STRICTER than the upstream author intended (review on #124).
    ({"$ref": "#/$defs/Thing", "required": ["name"]}, True, "ref-with-assertion-sibling"),
    # A bare `$ref` (nothing else) has no sibling to change meaning.
    ({"$ref": "#/$defs/Thing"}, False, "bare-ref"),
    # `$ref` beside PURE ANNOTATIONS (no validation behavior in either dialect) is fine.
    (
        {"$ref": "#/$defs/Thing", "$schema": _DRAFT_07, "title": "Thing"},
        False,
        "ref-with-annotation-siblings",
    ),
    # A `definitions`/`$defs` CONTAINER beside `$ref` is not an assertion — it constrains
    # nothing about the instance, and the reference resolves to the same target either way.
    # The extremely common "$ref to a local definition" shape must still normalize (review
    # on #124: treating every unlisted keyword as an assertion wrongly skipped it).
    (
        {"$ref": "#/definitions/Thing", "definitions": {"Thing": {"type": "object"}}},
        False,
        "ref-with-definitions-container",
    ),
    (
        {"$ref": "#/$defs/Thing", "$defs": {"Thing": {"type": "object"}}},
        False,
        "ref-with-defs-container",
    ),
    # ...but a real incompatibility INSIDE that container is still caught, since the walk
    # recurses through `definitions`/`$defs` independently of the `$ref`-sibling rule.
    (
        {"$ref": "#/definitions/T", "definitions": {"T": {"items": [{"type": "string"}]}}},
        True,
        "incompatibility-inside-definitions",
    ),
    # `$id` beside `$ref`: draft-07 ignores the sibling, so the reference resolves against the
    # INHERITED base URI; 2020-12 honours it and resolves beneath `sub/` — the same `$ref` can
    # reach a DIFFERENT schema after the relabel (review on #124).
    ({"$id": "sub/", "$ref": "thing"}, True, "ref-with-id-sibling"),
    # A SUBSCHEMA `$id` alone is enough: 2019-09 redefined it from "change the base URI within
    # this document" to "declare an embedded, independent schema resource".
    ({"properties": {"inner": {"$id": "sub/", "type": "object"}}}, True, "nested-id"),
    # ...but a ROOT `$id` is plain resource identity, and means the same in both dialects.
    ({"$id": "https://example.test/tool", "type": "object"}, False, "root-id"),
    # An empty fragment is still allowed under 2020-12, so it's equally safe.
    ({"$id": "https://example.test/tool#", "type": "object"}, False, "root-id-empty-fragment"),
    # A NON-EMPTY fragment is draft-07's plain-name anchor form, which 2020-12 forbids on
    # `$id` (that role moved to `$anchor`) — relabeling would leave an `$id` the 2020-12
    # meta-schema rejects, so the strict client keeps refusing the tool with a different
    # complaint instead of accepting it (review on #124).
    (
        {"$id": "https://example.test/tool#thing", "type": "object"},
        True,
        "root-id-plain-name-anchor",
    ),
    ({"$id": "#thing", "type": "object"}, True, "root-id-bare-anchor"),
    # `$anchor`/`$dynamicAnchor` postdate draft-07, so it ignores them as unknown keywords —
    # their value is never syntax-checked and they name nothing. 2020-12 both constrains their
    # syntax AND lets them take part in reference resolution, so the relabel can turn a valid
    # draft-07 schema into an invalid 2020-12 one, or retarget a `$ref` (review on #124).
    ({"type": "object", "$anchor": "thing"}, True, "anchor"),
    ({"type": "object", "$anchor": "bad anchor"}, True, "anchor-invalid-under-2020-12"),
    ({"type": "object", "$dynamicAnchor": "meta"}, True, "dynamic-anchor"),
    ({"properties": {"inner": {"$anchor": "thing"}}}, True, "nested-anchor"),
    # `$recursiveRef`/`$recursiveAnchor` are 2019-09-ONLY — 2020-12 replaced them with
    # `$dynamicRef`/`$dynamicAnchor` rather than keeping them, so they're unrecognized (and
    # inert) under BOTH dialects. Relabeling can't activate what neither one defines, so
    # skipping these would refuse a tool the toggle could have fixed (review on #124).
    ({"type": "object", "$recursiveRef": "#"}, False, "recursive-ref-is-inert-in-both"),
    ({"type": "object", "$recursiveAnchor": True}, False, "recursive-anchor-is-inert-in-both"),
    # `contentSchema` postdates draft-07, so draft-07 never meta-validates its value as a
    # schema and 2020-12 does — a draft-07-only construct hiding in there would turn the
    # client's "unsupported dialect" error into an "invalid schema" one. Walked, so skipped.
    (
        {"type": "string", "contentSchema": {"items": [{"type": "string"}]}},
        True,
        "incompatibility-inside-contentSchema",
    ),
    # A portable `contentSchema` is not itself a reason to skip.
    (
        {"type": "string", "contentSchema": {"type": "object"}},
        False,
        "portable-contentSchema",
    ),
    # `format` is a DELIBERATE exclusion, not an oversight — see the rationale beside
    # `_POST_DRAFT_07_ASSERTION_KEYWORDS`. draft-07 never guaranteed format-as-assertion (its
    # own spec makes it optional and opt-outable), so the relabel drops nothing the declared
    # dialect promised, and guarding it would refuse most real TypeScript-SDK tool schemas —
    # the very tools #123 is about. Pinned so the trade-off can't be reversed by accident.
    ({"type": "string", "format": "email"}, False, "format-is-deliberately-allowed"),
]

# Every post-draft-07 ASSERTION keyword, at the root and nested under a `properties` value.
# draft-07 ignores what it doesn't recognize, so these assert nothing under the declared
# dialect; the relabel switches them on and can start rejecting arguments the upstream tool
# accepted (successive review rounds on #124 surfaced these one at a time — the guard now
# catalogues the whole class, and this table pins every member of it).
_POST_DRAFT_07_SAMPLE_VALUES = {
    "unevaluatedProperties": False,
    "unevaluatedItems": False,
    "dependentRequired": {"credit_card": ["billing_address"]},
    "dependentSchemas": {"credit_card": {"type": "object"}},
    "minContains": 1,
    "maxContains": 3,
    "prefixItems": [{"type": "string"}],
    "$dynamicRef": "#meta",
}
for _kw, _value in _POST_DRAFT_07_SAMPLE_VALUES.items():
    _INCOMPATIBLE_CONSTRUCT_CASES.append(({"type": "object", _kw: _value}, True, f"root-{_kw}"))
    _INCOMPATIBLE_CONSTRUCT_CASES.append(
        ({"properties": {"inner": {_kw: _value}}}, True, f"nested-{_kw}")
    )


def test_post_draft07_keyword_table_is_fully_exercised():
    """The sample-value table above must cover every keyword the guard catalogues — adding a
    keyword to `_POST_DRAFT_07_ASSERTION_KEYWORDS` without a case here would ship untested."""
    assert set(_POST_DRAFT_07_SAMPLE_VALUES) == set(host._POST_DRAFT_07_ASSERTION_KEYWORDS)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [pytest.param(s, e, id=i) for s, e, i in _INCOMPATIBLE_CONSTRUCT_CASES],
)
def test_has_incompatible_draft07_construct(schema, expected):
    assert host._has_incompatible_draft07_construct(schema) is expected


def test_normalized_schema_only_touches_draft07():
    """draft-04's boolean-form `exclusiveMinimum` (superseded by draft-06's numeric form,
    which draft-07 keeps) is a real, different incompatibility this module doesn't
    catalogue — review on #124. Left untouched: normalization is scoped to draft-07 only,
    not "anything that isn't already 2020-12"."""
    draft04 = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "minimum": 0,
        "exclusiveMinimum": True,
    }
    assert host._ToolTransform._normalized_schema(draft04) is draft04
    # An unrecognized/custom dialect URI is likewise left alone.
    custom = {"$schema": "https://example.test/my-dialect", "type": "object"}
    assert host._ToolTransform._normalized_schema(custom) is custom


@pytest.mark.parametrize("uri", sorted(host._DRAFT_07_SCHEMA_URIS))
def test_normalized_schema_accepts_every_draft07_spelling(uri):
    """The set exists to tolerate generator variation (http vs https, with vs without the
    trailing `#` fragment). Each spelling must normalize, or the tolerance is fiction — only
    one of the four was exercised before (review on #124)."""
    schema = {"$schema": uri, "type": "object", "properties": {}}
    assert (
        host._ToolTransform._normalized_schema(schema)["$schema"] == host._SCHEMA_DIALECT_2020_12
    )


def test_normalized_schema_tolerates_a_non_string_dialect():
    """An untrusted upstream can advertise a malformed `$schema` (list, dict, number). A
    set-membership test against those raises `TypeError: unhashable type`, which would take
    `tools/list` down for the whole server — turning an opt-in compatibility toggle into an
    outage, where the default-off path merely passes the malformed schema through for the
    client to reject (review on #124). An unrecognized SHAPE is treated like an unrecognized
    STRING: left exactly as declared."""
    for bad in ({"$schema": ["draft-07"]}, {"$schema": {"uri": "x"}}, {"$schema": 7}):
        assert host._ToolTransform._normalized_schema(bad) is bad
