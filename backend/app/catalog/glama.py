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


def _repo_url(server: dict[str, Any]) -> str | None:
    """Read repository.url, tolerating a non-dict ``repository`` from a bad upstream."""
    repo = server.get("repository")
    return repo.get("url") if isinstance(repo, dict) else None


def _env_from_schema(schema: dict[str, Any], warnings: list[str]) -> dict[str, str]:
    """
    Build an environment variable scaffold from a Glama schema.
    
    Parameters:
    	schema (dict[str, Any]): The ``environmentVariablesJsonSchema`` value.
    	warnings (list[str]): Warning messages to extend for required variables.
    
    Returns:
    	dict[str, str]: An ``env`` mapping with one empty string entry per schema property.
    """
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
    req_list = schema.get("required")
    required = set(req_list) if isinstance(req_list, (list, set, tuple)) else set()
    env: dict[str, str] = {}
    for key in props:
        env[str(key)] = ""
        if key in required:
            warnings.append(f"Environment variable {key} is required — set its value before starting.")
    return env


def _list_item(server: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Glama server record into a directory listing item.
    
    Parameters:
    	server (dict[str, Any]): Glama server metadata.
    
    Returns:
    	dict[str, Any]: A directory item with Glama source fields and links.
    """
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
        "repository_url": _repo_url(server),
        "web_url": server.get("url"),
    }


def to_detail(server: dict[str, Any]) -> dict[str, Any]:
    """
    Create a manual-install detail record for a Glama server.
    
    Parameters:
    	server (dict[str, Any]): Glama server metadata.
    
    Returns:
    	dict[str, Any]: A detail payload containing the server metadata, a manual-install draft, notes, and no remotes.
    """
    name = str(server.get("name") or "")
    repo_url = _repo_url(server)
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
        """
        Initialize the Glama source cache.
        """
        self._cache = base.TTLCache()

    async def list_servers(
        self, http: httpx.AsyncClient, *, search: str | None, cursor: str | None, limit: int | None
    ) -> dict[str, Any]:
        """
        List Glama servers with optional search and pagination.
        
        Parameters:
        	search (str | None): Search text to filter servers.
        	cursor (str | None): Pagination cursor for the next page.
        	limit (int | None): Maximum number of servers to request.
        
        Returns:
        	dict[str, Any]: A dictionary containing the server list and the next pagination cursor.
        """
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
        """
        Fetches a Glama server's manual-install detail record.
        
        Parameters:
        	http (httpx.AsyncClient): HTTP client used for the request.
        	id (str): Opaque Glama server identifier.
        	version (str): Catalog version associated with the request.
        
        Returns:
        	dict[str, Any]: A manual-install detail record for the server.
        """
        url = f"{BASE_URL}/v1/servers/{quote(id, safe='')}"
        data = await base.get_json(http, url, {})
        if not isinstance(data, dict):
            raise base.CatalogUpstreamError("unexpected detail response from the Glama directory")
        return to_detail(data)
