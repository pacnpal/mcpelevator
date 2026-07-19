"""Bridge REST/OpenAPI surface tests: the routes ``build_app`` adds when a server's
``rest_openapi`` exposure is on.

A real (in-memory) FastMCP server with tools backs the routes, so these exercise
the actual call path — Starlette route → in-memory FastMCP client session → tool.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from fastmcp import FastMCP

from app.bridge.host import build_app


def _upstream() -> FastMCP:
    mcp = FastMCP("rest-upstream")

    @mcp.tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @mcp.tool
    def fail() -> str:
        """Always raises."""
        raise ValueError("nope")

    return mcp


def _client(spec: dict) -> TestClient:
    return TestClient(build_app(spec, _upstream()))


REST_ON = {"name": "rest-upstream", "rest_openapi": True}


def test_rest_routes_absent_when_exposure_off():
    with _client({"name": "x", "rest_openapi": False}) as client:
        assert client.get("/rest").status_code == 404
        assert client.get("/rest/openapi.json").status_code == 404


def test_rest_index_lists_tools():
    with _client(REST_ON) as client:
        r = client.get("/rest")
        assert r.status_code == 200
        body = r.json()
        names = {t["name"] for t in body["tools"]}
        assert names == {"add", "fail"}
        assert body["openapi"] == "rest/openapi.json"


def test_openapi_document_covers_tools_with_schemas():
    with _client(REST_ON) as client:
        r = client.get("/rest/openapi.json")
        assert r.status_code == 200
        doc = r.json()
        assert doc["openapi"].startswith("3.1")
        assert doc["servers"] == [{"url": "../"}]
        op = doc["paths"]["/rest/add"]["post"]
        assert op["operationId"] == "add"
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert set(schema["properties"]) == {"a", "b"}


def test_rest_call_returns_envelope():
    with _client(REST_ON) as client:
        r = client.post("/rest/add", json={"a": 2, "b": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_error"] is False
        assert body["structured_content"] == {"result": 5}
        assert any(c.get("text") == "5" for c in body["content"])


def test_rest_call_empty_body_means_no_arguments():
    """A tool with no required args must be callable with an empty POST body."""
    mcp = FastMCP("noargs")

    @mcp.tool
    def ping() -> str:
        return "pong"

    with TestClient(build_app(REST_ON, mcp)) as client:
        r = client.post("/rest/ping")
        assert r.status_code == 200, r.text
        assert r.json()["structured_content"] == {"result": "pong"}


def test_rest_call_tool_error_mirrors_mcp_semantics():
    with _client(REST_ON) as client:
        r = client.post("/rest/fail", json={})
        assert r.status_code == 200  # transport OK; the tool itself failed
        assert r.json()["is_error"] is True


def test_rest_call_unknown_tool_404():
    with _client(REST_ON) as client:
        r = client.post("/rest/missing", json={})
        assert r.status_code == 404
        assert "missing" in r.text


def test_rest_call_rejects_non_object_body():
    with _client(REST_ON) as client:
        assert client.post("/rest/add", json=[1, 2]).status_code == 400
        assert (
            client.post("/rest/add", content=b"not json").status_code == 400
        )


def test_mcp_surface_still_served_alongside_rest():
    """The /mcp Streamable-HTTP route must coexist with the REST routes."""
    with _client(REST_ON) as client:
        # A GET without an MCP session is rejected by the transport, but the route
        # exists (not the SPA-style 404 an unknown path gets).
        r = client.get("/mcp")
        assert r.status_code != 404
