"""Official MCP Registry source — ``registry.modelcontextprotocol.io``.

A package-based directory: ``server.json`` documents carry ``packages[]`` with enough
to build a runnable command, so installs are *auto* (npm → npx, pypi → uvx via the
shared ``mapping`` core). List/detail entries are wrapped as
``{"server": {...}, "_meta": {...}}`` with the moderation ``status`` under ``_meta``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.catalog import base, mapping

BASE_URL = "https://registry.modelcontextprotocol.io"
_META_KEY = "io.modelcontextprotocol.registry/official"


def _unwrap(entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Split a registry entry into (server.json, status). Tolerates a bare document."""
    if isinstance(entry.get("server"), dict):
        server = entry["server"]
        official = (entry.get("_meta") or {}).get(_META_KEY) or {}
        return server, str(official.get("status") or "active")
    return entry, str(entry.get("status") or "active")


def _list_item(entry: dict[str, Any]) -> dict[str, Any]:
    server, status = _unwrap(entry)
    name = str(server.get("name") or "")
    version = server.get("version")
    packages = server.get("packages") or []

    registry_types: list[str] = []
    installable = False
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        rtype = str(pkg.get("registryType") or "").lower()
        if rtype and rtype not in registry_types:
            registry_types.append(rtype)
        transport_type = str((pkg.get("transport") or {}).get("type") or "stdio").lower()
        if transport_type == "stdio" and rtype in mapping.RUNNER_BY_TYPE and pkg.get("identifier"):
            installable = True

    return {
        "source": "official",
        "id": name,
        "name": name,
        "title": server.get("title") or name,
        "description": server.get("description") or "",
        "version": None if version in (None, "") else str(version),
        "status": status,
        "registry_types": registry_types,
        "installable": installable,
        "repository_url": (server.get("repository") or {}).get("url"),
        "web_url": None,
    }


def to_detail(entry: dict[str, Any]) -> dict[str, Any]:
    """Resolve an official registry document into install drafts + metadata (pure)."""
    server, status = _unwrap(entry)
    name = str(server.get("name") or "")
    version = server.get("version")
    packages = server.get("packages") or []
    remotes = server.get("remotes") or []

    drafts = [mapping.package_draft(i, pkg) for i, pkg in enumerate(packages) if isinstance(pkg, dict)]

    return {
        "source": "official",
        "manual_install": False,
        "notes": [],
        "server": {
            "name": name,
            "title": server.get("title") or name,
            "description": server.get("description") or "",
            "version": None if version in (None, "") else str(version),
            "status": status,
            "repository_url": (server.get("repository") or {}).get("url"),
            "web_url": None,
        },
        "drafts": drafts,
        "remotes": [
            {"type": str(r.get("type") or ""), "url": str(r.get("url") or "")}
            for r in remotes
            if isinstance(r, dict)
        ],
    }


class OfficialSource:
    id = "official"
    label = "MCP Registry"
    install_support = "auto"

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
            http, f"{BASE_URL}/v0.1/servers", {"search": search, "cursor": cursor, "limit": page}
        )
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, list):
            raise base.CatalogUpstreamError("unexpected list response from the MCP Registry")
        metadata = data.get("metadata") or {}
        result = {
            "servers": [_list_item(e) for e in servers if isinstance(e, dict)],
            "next_cursor": metadata.get("nextCursor"),
        }
        self._cache.put(key, result)
        return result

    async def get_detail(
        self, http: httpx.AsyncClient, *, id: str, version: str
    ) -> dict[str, Any]:
        # Path params must be URL-encoded (e.g. ``io.x/name`` → ``io.x%2Fname``).
        enc_name = quote(id, safe="")
        enc_version = quote(version or "latest", safe="")
        url = f"{BASE_URL}/v0.1/servers/{enc_name}/versions/{enc_version}"
        data = await base.get_json(http, url, {})
        if not isinstance(data, dict):
            raise base.CatalogUpstreamError("unexpected detail response from the MCP Registry")
        return to_detail(data)
