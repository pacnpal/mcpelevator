"""openapi runner, bridge side: an OpenAPI document translated into MCP tools.

A real FastAPI app stands in for the REST API (reached through an in-process ASGI
transport) and its generated document is served by a mock transport, so these exercise
the actual path — document fetch → FastMCP's OpenAPIProvider → the bridge's tool-policy
transform → an HTTP request to the API.
"""

from __future__ import annotations

import functools
import json

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.testclient import TestClient
from fastmcp import Client

from app.bridge import openapi
from app.bridge.host import UPSTREAM_META_KEY, build_app, build_proxy

DOC_URL = "https://pets.example/openapi.json"


def _api() -> tuple[FastAPI, list, FastAPI]:
    """A small pet API mounted under /v1 (returned as the outer app, the request log, and
    the API itself, whose generated document the tests serve)."""
    api = FastAPI(title="Pets")
    seen: list = []

    @api.middleware("http")
    async def record(request: Request, call_next):
        seen.append((request.method, request.url.path, request.headers.get("x-api-key")))
        api.state.last_headers = dict(request.headers)
        return await call_next(request)

    @api.get("/pets/{pet_id}", operation_id="getPet")
    def get_pet(pet_id: str):
        """Fetch one pet."""
        return {"id": pet_id, "name": "rex"}

    @api.get("/pets", operation_id="listPets")
    def list_pets(limit: int = 10):
        return {"pets": [], "limit": limit}

    @api.get("/me", operation_id="whoAmI")
    def who_am_i(x_api_key: str = Header()):
        """Declares its API key as a header parameter, as many documents do."""
        return {"key": x_api_key}

    @api.delete("/pets/{pet_id}", operation_id="deletePet")
    def delete_pet(pet_id: str):
        return {"deleted": pet_id}

    outer = FastAPI()
    outer.mount("/v1", api)
    return outer, seen, api


def _proxy(monkeypatch, **spec_overrides):
    outer, seen, api = _api()
    doc = api.openapi()
    doc["servers"] = [{"url": "/v1"}]  # relative: resolved against the document's URL
    monkeypatch.setattr(
        openapi,
        "build_server",
        functools.partial(
            openapi.build_server,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=doc)),
            api_transport=httpx.ASGITransport(app=outer),
        ),
    )
    spec = {
        "command": DOC_URL,
        "args": [],
        "env": {"X-Api-Key": "k1"},
        "transport": "openapi",
        "name": "pets",
        **spec_overrides,
    }
    return build_proxy(spec), seen, spec, api


# --- the translated server ------------------------------------------------------------


async def test_operations_become_callable_tools(monkeypatch):
    proxy, seen, _, _ = _proxy(monkeypatch)
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"getPet", "listPets", "deletePet", "whoAmI"}
        assert tools["getPet"].inputSchema["required"] == ["pet_id"]
        result = await client.call_tool("getPet", {"pet_id": "7"})
    assert result.structured_content == {"id": "7", "name": "rex"}
    # The relative server url resolved onto the document's host, and headers rode along.
    assert seen == [("GET", "/v1/pets/7", "k1")]


async def test_tool_policy_applies_to_translated_tools(monkeypatch):
    proxy, seen, _, _ = _proxy(
        monkeypatch,
        disabled_tools=["deletePet", "whoAmI"],
        tool_overrides={"getPet": {"name": "fetch_pet", "description": "Look up a pet"}},
    )
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"fetch_pet", "listPets"}
        assert tools["fetch_pet"].description == "Look up a pet"
        # The renamed tool carries its pre-rename identity for the discovery probe.
        assert tools["fetch_pet"].meta[UPSTREAM_META_KEY] == {"name": "getPet"}
        assert (await client.call_tool("fetch_pet", {"pet_id": "1"})).structured_content["id"] == "1"
        hidden = await client.call_tool("deletePet", {"pet_id": "1"}, raise_on_error=False)
        assert hidden.is_error
    assert ("DELETE", "/v1/pets/1", "k1") not in seen  # a hidden operation never reaches the API


async def test_path_parameters_cannot_traverse(monkeypatch):
    proxy, seen, _, _ = _proxy(monkeypatch)
    async with Client(proxy) as client:
        result = await client.call_tool("getPet", {"pet_id": "../pets"}, raise_on_error=False)
    assert result.is_error
    assert seen == []


async def test_configured_header_is_not_a_tool_argument_and_always_wins(monkeypatch):
    proxy, _, _, _ = _proxy(monkeypatch, env={"X-API-KEY": "operator-key"})
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
        # The document declares x-api-key on the operation; the operator already supplies it.
        assert "x-api-key" not in tools["whoAmI"].inputSchema.get("properties", {})
        assert "x-api-key" not in tools["whoAmI"].inputSchema.get("required", [])
        result = await client.call_tool("whoAmI", {"x-api-key": "model-guess"})
    assert result.structured_content == {"key": "operator-key"}


async def test_unconfigured_header_parameter_stays_a_tool_argument(monkeypatch):
    proxy, _, _, _ = _proxy(monkeypatch, env={})
    async with Client(proxy) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "x-api-key" in tools["whoAmI"].inputSchema["required"]
        result = await client.call_tool("whoAmI", {"x-api-key": "caller-supplied"})
    assert result.structured_content == {"key": "caller-supplied"}


async def test_caller_request_headers_never_reach_the_api(monkeypatch):
    """The MCP request the bridge serves is the caller's, relayed by the /s proxy; none of
    its headers may ride along to the third-party API (only the configured ones)."""
    from fastmcp.server.dependencies import _current_http_request
    from starlette.requests import Request as StarletteRequest

    proxy, _, _, api = _proxy(monkeypatch)
    caller = StarletteRequest({
        "type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
        "headers": [
            (b"cf-access-jwt-assertion", b"signed-caller-jwt"),
            (b"x-forwarded-for", b"203.0.113.9"),
            (b"x-caller-secret", b"s3cret"),
        ],
    })
    token = _current_http_request.set(caller)
    try:
        async with Client(proxy) as client:
            await client.call_tool("getPet", {"pet_id": "1"})
    finally:
        _current_http_request.reset(token)
    sent = api.state.last_headers
    assert sent["x-api-key"] == "k1"  # the configured header
    for leaked in ("cf-access-jwt-assertion", "x-forwarded-for", "x-caller-secret"):
        assert leaked not in sent


def test_rest_mirror_serves_translated_tools(monkeypatch):
    proxy, _, spec, _ = _proxy(monkeypatch, rest_openapi=True)
    with TestClient(build_app(spec, proxy)) as client:
        r = client.post("/rest/listPets", json={"limit": 3})
        assert r.status_code == 200
        assert r.json()["structured_content"] == {"pets": [], "limit": 3}


# --- document parsing -----------------------------------------------------------------


def test_parse_accepts_json_and_yaml():
    doc = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": {}}
    assert openapi.parse_document(json.dumps(doc).encode()) == doc
    yaml_doc = b"openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n"
    assert openapi.parse_document(yaml_doc)["openapi"] == "3.1.0"


@pytest.mark.parametrize(
    "raw, message",
    [
        (b'{"swagger": "2.0", "paths": {}}', "convert it to OpenAPI 3"),
        (b'{"openapi": "4.0", "paths": {}}', "not an OpenAPI 3 document"),
        (b"[1, 2]", "must be a JSON/YAML object"),
        (b"{: not yaml", "neither JSON nor YAML"),
    ],
)
def test_parse_refuses_untranslatable_documents(raw, message):
    with pytest.raises(ValueError, match=message):
        openapi.parse_document(raw)


@pytest.mark.parametrize(
    "servers, override, expected",
    [
        (None, "https://override.example/v9", "https://override.example/v9"),
        ([{"url": "https://api.example/v1"}], None, "https://api.example/v1"),
        ([{"url": "/api/v3"}], None, "https://docs.example/api/v3"),
        (None, None, "https://docs.example/"),  # OpenAPI's default: one server at "/"
        (
            [{"url": "https://{region}.api.example/{v}", "variables": {
                "region": {"default": "eu"}, "v": {"default": "v2"}}}],
            None,
            "https://eu.api.example/v2",
        ),
    ],
)
def test_resolve_base_url(servers, override, expected):
    doc = {"openapi": "3.1.0", "paths": {}}
    if servers is not None:
        doc["servers"] = servers
    assert openapi.resolve_base_url(doc, "https://docs.example/spec.json", override) == expected


def test_resolve_base_url_refuses_non_http_server():
    doc = {"openapi": "3.1.0", "paths": {}, "servers": [{"url": "ftp://files.example"}]}
    with pytest.raises(ValueError, match="base URL override"):
        openapi.resolve_base_url(doc, "https://docs.example/spec.json", None)


# --- document fetch -------------------------------------------------------------------


def test_fetch_sends_no_credentials_to_a_public_document():
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"{}")

    openapi.fetch_document(DOC_URL, {"X-Api-Key": "k"}, transport=httpx.MockTransport(handler))
    assert [r.headers.get("x-api-key") for r in requests] == [None]


def test_fetch_retries_a_protected_document_with_credentials():
    def handler(request):
        if request.headers.get("x-api-key") != "k":
            return httpx.Response(401)
        return httpx.Response(200, content=b"spec")

    raw, served_from = openapi.fetch_document(
        DOC_URL, {"X-Api-Key": "k"}, transport=httpx.MockTransport(handler)
    )
    assert (raw, served_from) == (b"spec", DOC_URL)


def test_fetch_never_follows_a_redirect_with_credentials():
    def handler(request):
        if request.headers.get("x-api-key"):
            return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        openapi.fetch_document(DOC_URL, {"X-Api-Key": "k"}, transport=httpx.MockTransport(handler))


def test_unfetchable_document_is_a_document_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(openapi.DocumentError, match="can't fetch the OpenAPI document"):
        openapi.build_server({"command": DOC_URL}, transport=transport)


def test_bridge_exits_with_one_line_on_a_bad_document(monkeypatch):
    from app.bridge import host

    def bad(spec):
        raise openapi.DocumentError("not an OpenAPI 3 document (openapi: 'missing')")

    monkeypatch.setattr(openapi, "build_server", bad)
    monkeypatch.setenv("MCPE_BRIDGE_SPEC", json.dumps({"command": DOC_URL, "transport": "openapi"}))
    monkeypatch.setenv("MCPE_BRIDGE_PORT", "0")
    with pytest.raises(SystemExit) as exited:
        host.main()
    assert str(exited.value.code) == "openapi: not an OpenAPI 3 document (openapi: 'missing')"


async def test_relative_servers_resolve_against_the_redirected_document():
    """OpenAPI resolves a relative server URL against where the document actually lives,
    so a redirected document's API is on the redirect target, not the entry URL."""
    doc = {
        "openapi": "3.1.0", "info": {"title": "t", "version": "1"},
        "servers": [{"url": "./api"}],
        "paths": {"/pets": {"get": {"operationId": "listPets",
                                    "responses": {"200": {"description": "ok"}}}}},
    }

    def serve_document(request):
        if request.url.host == "entry.example":
            return httpx.Response(302, headers={"location": "https://actual.example/v2/openapi.json"})
        return httpx.Response(200, json=doc)

    called: list[str] = []

    def api(request):
        called.append(str(request.url))
        return httpx.Response(200, json={"pets": []})

    server = openapi.build_server(
        {"command": "https://entry.example/spec"},
        transport=httpx.MockTransport(serve_document),
        api_transport=httpx.MockTransport(api),
    )
    async with Client(server) as client:
        await client.call_tool("listPets", {})
    assert called == ["https://actual.example/v2/api/pets"]


def test_fetch_caps_document_size(monkeypatch):
    monkeypatch.setattr(openapi, "MAX_DOCUMENT_BYTES", 10)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(ValueError, match="larger than"):
        openapi.fetch_document(DOC_URL, {}, transport=transport)
