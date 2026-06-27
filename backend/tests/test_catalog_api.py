"""Catalog API tests — browse/detail endpoints with a stubbed upstream directory.

The real client functions (and thus the full mapping + normalization path) run; only
``_get_json`` — the single outbound HTTP call — is stubbed, keyed by URL.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.catalog import base, registry
from app.main import app

LOOPBACK = {"host": "127.0.0.1"}

# A registry list page (wrapped entries) and a detail document.
_OFFICIAL_LIST = {
    "servers": [
        {
            "server": {
                "name": "io.example/memory",
                "title": "Memory",
                "description": "remember things",
                "version": "1.0.0",
                "packages": [
                    {"registryType": "npm", "identifier": "@me/memory", "version": "1.0.0",
                     "transport": {"type": "stdio"}}
                ],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
        }
    ],
    "metadata": {"nextCursor": "io.example/memory:1.0.0", "count": 1},
}
_OFFICIAL_DETAIL = {
    "server": {
        "name": "io.example/memory",
        "title": "Memory",
        "version": "1.0.0",
        "packages": [
            {"registryType": "npm", "identifier": "@me/memory", "version": "1.0.0",
             "transport": {"type": "stdio"}}
        ],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
}
_GLAMA_LIST = {
    "pageInfo": {"endCursor": "cur2", "hasNextPage": True, "hasPreviousPage": False},
    "servers": [{"id": "abc", "name": "cool", "description": "d",
                 "repository": {"url": "https://github.com/x/cool"},
                 "url": "https://glama.ai/mcp/servers/abc",
                 "environmentVariablesJsonSchema": {"properties": {}, "required": []}}],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """
    Clear cached catalog source data before and after each test.
    """
    def clear():
        for src in registry.SOURCES.values():
            cache = getattr(src, "_cache", None)
            if cache is not None:
                cache.clear()

    clear()
    yield
    clear()


def _stub(monkeypatch, routes: dict):
    """
    Stub upstream catalog responses by matching URL substrings.
    
    Parameters:
    	routes (dict): A mapping of URL substrings to JSON payloads.
    """
    async def fake_get_json(http, url, params):
        for needle, payload in routes.items():
            if needle in url:
                return payload
        raise AssertionError(f"unexpected upstream URL: {url}")

    # Sources call base.get_json via the module attribute, so patching it here is enough.
    monkeypatch.setattr(base, "get_json", fake_get_json)


def test_sources_lists_official_and_glama():
    with TestClient(app) as c:
        r = c.get("/api/catalog/sources", headers=LOOPBACK)
        assert r.status_code == 200, r.text
        by_id = {s["id"]: s for s in r.json()}
        assert by_id["official"]["install_support"] == "auto"
        assert by_id["glama"]["install_support"] == "manual"


def test_official_list_passthrough_and_cursor(monkeypatch):
    _stub(monkeypatch, {"registry.modelcontextprotocol.io/v0.1/servers": _OFFICIAL_LIST})
    with TestClient(app) as c:
        r = c.get("/api/catalog/servers", params={"search": "memory"}, headers=LOOPBACK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "official"
        assert body["next_cursor"] == "io.example/memory:1.0.0"
        assert body["servers"][0]["installable"] is True
        assert body["servers"][0]["registry_types"] == ["npm"]


def test_official_detail_resolves_drafts(monkeypatch):
    _stub(monkeypatch, {"/versions/": _OFFICIAL_DETAIL})
    with TestClient(app) as c:
        r = c.get("/api/catalog/server", params={"id": "io.example/memory"}, headers=LOOPBACK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["manual_install"] is False
        draft = body["drafts"][0]
        assert draft["runner"] == "npx"
        assert draft["args"] == ["-y", "@me/memory@1.0.0"]


def test_glama_list_passthrough(monkeypatch):
    _stub(monkeypatch, {"glama.ai/api/mcp/v1/servers": _GLAMA_LIST})
    with TestClient(app) as c:
        r = c.get("/api/catalog/servers", params={"source": "glama"}, headers=LOOPBACK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["next_cursor"] == "cur2"
        assert body["servers"][0]["source"] == "glama"
        assert body["servers"][0]["installable"] is False


def test_unknown_source_is_400():
    with TestClient(app) as c:
        r = c.get("/api/catalog/servers", params={"source": "nope"}, headers=LOOPBACK)
        assert r.status_code == 400, r.text


def test_upstream_error_is_502(monkeypatch):
    async def boom(http, url, params):
        raise base.CatalogUpstreamError("connection refused")

    monkeypatch.setattr(base, "get_json", boom)
    with TestClient(app) as c:
        r = c.get("/api/catalog/servers", headers=LOOPBACK)
        assert r.status_code == 502, r.text


def test_detail_404_passthrough(monkeypatch):
    import httpx

    async def not_found(http, url, params):
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(base, "get_json", not_found)
    with TestClient(app) as c:
        r = c.get("/api/catalog/server", params={"id": "io.example/missing"}, headers=LOOPBACK)
        assert r.status_code == 404, r.text
