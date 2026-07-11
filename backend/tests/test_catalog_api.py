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
    "servers": [{"id": "abc", "namespace": "acme", "slug": "cool", "name": "cool",
                 "description": "d",
                 "repository": {"url": "https://github.com/x/cool"},
                 "url": "https://glama.ai/mcp/servers/abc",
                 "environmentVariablesJsonSchema": {"properties": {}, "required": []}}],
}
_GLAMA_DETAIL = {"id": "abc", "namespace": "acme", "slug": "cool", "name": "cool",
                 "description": "d", "repository": {"url": "https://github.com/x/cool"},
                 "url": "https://glama.ai/mcp/servers/abc",
                 "environmentVariablesJsonSchema": {"properties": {}, "required": []}}


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

    Returns:
    	list: Captured (url, params) tuples for every outbound call, so tests can
    	assert the forwarded search/cursor/limit values.
    """
    calls: list[tuple[str, dict]] = []

    async def fake_get_json(http, url, params):
        calls.append((url, params))
        for needle, payload in routes.items():
            if needle in url:
                return payload
        raise AssertionError(f"unexpected upstream URL: {url}")

    # Sources call base.get_json via the module attribute, so patching it here is enough.
    monkeypatch.setattr(base, "get_json", fake_get_json)
    return calls


def test_official_list_item_surfaces_remote_type_and_installable():
    from app.catalog import official

    item = official._list_item(
        {
            "server": {
                "name": "io.x/remote",
                "title": "Remote",
                "remotes": [{"type": "streamable-http", "url": "https://up/mcp"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
        }
    )
    # The proxy can run a remote upstream, so it's installable and carries a "remote" type
    # for the browse facet.
    assert item["installable"] is True
    assert "remote" in item["registry_types"]


def test_official_list_item_unsupported_remote_is_not_installable():
    from app.catalog import official

    item = official._list_item(
        {
            "server": {
                "name": "io.x/ws",
                "remotes": [{"type": "websocket", "url": "wss://up/ws"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
        }
    )
    # The remote runner can't proxy websocket — don't surface it as installable/remote.
    assert item["installable"] is False
    assert "remote" not in item["registry_types"]


def test_official_list_item_non_string_remote_type_does_not_crash():
    from app.catalog import official

    # A malformed registry record (non-string `type`) must be treated as unsupported,
    # not raise and break the whole catalog page.
    item = official._list_item(
        {
            "server": {"name": "io.x/bad", "remotes": [{"type": 5, "url": "https://up/mcp"}]},
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
        }
    )
    assert item["installable"] is False
    assert "remote" not in item["registry_types"]


def test_official_list_item_deleted_remote_is_not_installable():
    from app.catalog import official

    item = official._list_item(
        {
            "server": {
                "name": "io.x/bad",
                "remotes": [{"type": "sse", "url": "https://up/sse"}],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deleted"}},
        }
    )
    assert item["installable"] is False


def test_sources_lists_official_and_glama():
    with TestClient(app) as c:
        r = c.get("/api/catalog/sources", headers=LOOPBACK)
        assert r.status_code == 200, r.text
        by_id = {s["id"]: s for s in r.json()}
        assert by_id["official"]["install_support"] == "auto"
        assert by_id["glama"]["install_support"] == "manual"


def test_official_list_passthrough_and_cursor(monkeypatch):
    calls = _stub(monkeypatch, {"registry.modelcontextprotocol.io/v0.1/servers": _OFFICIAL_LIST})
    with TestClient(app) as c:
        r = c.get(
            "/api/catalog/servers",
            params={"search": "memory", "cursor": "io.example/a:1.0.0", "limit": 25},
            headers=LOOPBACK,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "official"
        assert body["next_cursor"] == "io.example/memory:1.0.0"
        assert body["servers"][0]["installable"] is True
        assert body["servers"][0]["registry_types"] == ["npm"]
        # The query/cursor/limit must be forwarded to the upstream (pagination contract).
        _, params = calls[0]
        assert params["search"] == "memory"
        assert params["cursor"] == "io.example/a:1.0.0"
        assert params["limit"] == 25


_OFFICIAL_LIST_MULTIVERSION = {
    "servers": [
        {"server": {"name": "io.x/srv", "title": "Srv", "version": "1.0.0", "packages": []},
         "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": False}}},
        {"server": {"name": "io.x/srv", "title": "Srv", "version": "1.0.1", "packages": []},
         "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}}},
    ],
    "metadata": {"nextCursor": None},
}


def test_official_list_dedupes_to_latest_version(monkeypatch):
    calls = _stub(monkeypatch, {"registry.modelcontextprotocol.io/v0.1/servers": _OFFICIAL_LIST_MULTIVERSION})
    with TestClient(app) as c:
        body = c.get("/api/catalog/servers", headers=LOOPBACK).json()
        # Upstream is asked for latest-only; dedupe still collapses any residual dups.
        assert calls[0][1].get("version") == "latest"
        assert len(body["servers"]) == 1
        assert body["servers"][0]["version"] == "1.0.1"


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


_OFFICIAL_VERSIONS = {
    "servers": [
        {"server": {"name": "io.x/srv", "version": "1.0.1"},
         "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}}},
        {"server": {"name": "io.x/srv", "version": "1.0.0"},
         "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": False}}},
    ]
}


def test_official_versions_endpoint_latest_first(monkeypatch):
    _stub(monkeypatch, {"/versions": _OFFICIAL_VERSIONS})
    with TestClient(app) as c:
        r = c.get("/api/catalog/server/versions", params={"id": "io.x/srv"}, headers=LOOPBACK)
        assert r.status_code == 200, r.text
        assert r.json()["versions"] == ["1.0.1", "1.0.0"]


def test_official_versions_malformed_payload_is_502(monkeypatch):
    _stub(monkeypatch, {"/versions": {"unexpected": "shape"}})
    with TestClient(app) as c:
        r = c.get("/api/catalog/server/versions", params={"id": "io.x/srv"}, headers=LOOPBACK)
        assert r.status_code == 502, r.text


def test_glama_versions_endpoint_is_empty():
    # Glama has no version concept; the endpoint returns [] without hitting upstream.
    with TestClient(app) as c:
        r = c.get("/api/catalog/server/versions", params={"source": "glama", "id": "a/b"}, headers=LOOPBACK)
        assert r.status_code == 200, r.text
        assert r.json()["versions"] == []


def test_official_malformed_transport_and_repository_do_not_crash(monkeypatch):
    malformed = {
        "servers": [
            {
                "server": {
                    "name": "io.example/bad",
                    "version": "1.0.0",
                    "repository": "not-an-object",
                    "packages": [
                        {
                            "registryType": "npm",
                            "identifier": "@me/bad",
                            "version": "1.0.0",
                            "transport": "not-an-object",
                        }
                    ],
                },
                "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
            }
        ],
        "metadata": "not-an-object",
    }
    _stub(monkeypatch, {"registry.modelcontextprotocol.io/v0.1/servers": malformed})

    with TestClient(app) as c:
        r = c.get("/api/catalog/servers", headers=LOOPBACK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["next_cursor"] is None
        assert body["servers"][0]["repository_url"] is None
        assert body["servers"][0]["installable"] is True


def test_official_detail_malformed_transport_and_repository_do_not_crash(monkeypatch):
    malformed = {
        "server": {
            "name": "io.example/bad",
            "version": "1.0.0",
            "repository": "not-an-object",
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": "@me/bad",
                    "version": "1.0.0",
                    "transport": "not-an-object",
                }
            ],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
    }
    _stub(monkeypatch, {"/versions/": malformed})

    with TestClient(app) as c:
        r = c.get("/api/catalog/server", params={"id": "io.example/bad"}, headers=LOOPBACK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["server"]["repository_url"] is None
        assert body["drafts"][0]["installable"] is True
        assert body["drafts"][0]["runner"] == "npx"

def test_glama_list_passthrough(monkeypatch):
    calls = _stub(monkeypatch, {"glama.ai/api/mcp/v1/servers": _GLAMA_LIST})
    with TestClient(app) as c:
        r = c.get(
            "/api/catalog/servers",
            params={"source": "glama", "search": "git", "cursor": "cur1"},
            headers=LOOPBACK,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["next_cursor"] == "cur2"
        assert body["servers"][0]["source"] == "glama"
        assert body["servers"][0]["installable"] is False
        # The lookup id is the stable namespace/slug, not the deprecated opaque id.
        assert body["servers"][0]["id"] == "acme/cool"
        # Glama uses its own param names; the source must translate to query/after.
        _, params = calls[0]
        assert params["query"] == "git"
        assert params["after"] == "cur1"


def test_glama_detail_uses_namespace_slug_route(monkeypatch):
    calls = _stub(monkeypatch, {"glama.ai/api/mcp/v1/servers/": _GLAMA_DETAIL})
    with TestClient(app) as c:
        r = c.get(
            "/api/catalog/server",
            params={"source": "glama", "id": "acme/cool"},
            headers=LOOPBACK,
        )
        assert r.status_code == 200, r.text
        assert r.json()["manual_install"] is True
        # The slash stays a path separator on the documented detail route.
        url, _ = calls[0]
        assert url.endswith("/v1/servers/acme/cool")


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
