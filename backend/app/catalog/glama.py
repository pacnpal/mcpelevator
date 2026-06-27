"""Glama directory source — ``glama.ai/api/mcp``.

A *discovery-only* directory: entries are flat metadata (name, description, repository,
``environmentVariablesJsonSchema``) with **no launch spec**, so installs are *manual*.
We scaffold the name + required env-var keys and link to the repo; the operator supplies
the runner/package/command in the review form.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.catalog import base, mapping

BASE_URL = "https://glama.ai/api/mcp"


def _env_from_schema(schema: dict[str, Any], warnings: list[str]) -> dict[str, str]:
    """Scaffold an ``env`` dict from Glama's ``environmentVariablesJsonSchema`` (ordered)."""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    env: dict[str, str] = {}
    for key in props:
        env[str(key)] = ""
        if key in required:
            warnings.append(f"Environment variable {key} is required — set its value before starting.")
    return env


def _list_item(server: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "glama",
        "id": str(server.get("id") or ""),
        "name": str(server.get("name") or ""),
        "title": str(server.get("name") or ""),
        "description": str(server.get("description") or ""),
        "version": None,
        "status": "active",
        # Glama doesn't publish package types; nothing is auto-installable from it.
        "registry_types": [],
        "installable": False,
        "repository_url": (server.get("repository") or {}).get("url"),
        "web_url": server.get("url"),
    }


def to_detail(server: dict[str, Any]) -> dict[str, Any]:
    """Resolve a Glama server into a *manual* install scaffold (pure)."""
    name = str(server.get("name") or "")
    repo_url = (server.get("repository") or {}).get("url")
    warnings: list[str] = []
    env = _env_from_schema(server.get("environmentVariablesJsonSchema") or {}, warnings)

    draft = mapping.blank_draft(0, "unknown", "", None)
    draft["env"] = env
    draft["warnings"] = warnings
    draft["reason"] = "Glama doesn't publish a launch command — enter the package/command manually."

    notes = ["Listed in the Glama directory — set the runner and package/command yourself."]
    if repo_url:
        notes.append(f"Install instructions are usually in the repository: {repo_url}")

    return {
        "source": "glama",
        "manual_install": True,
        "notes": notes,
        "server": {
            "name": name,
            "title": name,
            "description": str(server.get("description") or ""),
            "version": None,
            "status": "active",
            "repository_url": repo_url,
            "web_url": server.get("url"),
        },
        "drafts": [draft],
        "remotes": [],
    }


class GlamaSource:
    id = "glama"
    label = "Glama"
    install_support = "manual"

    def __init__(self) -> None:
        self._cache = base.TTLCache()

    async def list_servers(
        self, http: httpx.AsyncClient, *, search: str | None, cursor: str | None, limit: int | None
    ) -> dict[str, Any]:
        page = base.clamp_limit(limit)
        key = f"list:{search}:{cursor}:{page}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = await base.get_json(
            http, f"{BASE_URL}/v1/servers", {"query": search, "after": cursor, "first": page}
        )
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, list):
            raise base.CatalogUpstreamError("unexpected list response from the Glama directory")
        page_info = data.get("pageInfo") or {}
        next_cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
        result = {
            "servers": [_list_item(s) for s in servers if isinstance(s, dict)],
            "next_cursor": next_cursor,
        }
        self._cache.put(key, result)
        return result

    async def get_detail(
        self, http: httpx.AsyncClient, *, id: str, version: str
    ) -> dict[str, Any]:
        # Glama's detail key is the opaque server id carried in the list item's ``id``.
        url = f"{BASE_URL}/v1/servers/{quote(id, safe='')}"
        data = await base.get_json(http, url, {})
        if not isinstance(data, dict):
            raise base.CatalogUpstreamError("unexpected detail response from the Glama directory")
        return to_detail(data)
