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
    """
    Extract the server document and status from a registry entry.
    
    Parameters:
    	entry (dict[str, Any]): A registry entry or a bare server document.
    
    Returns:
    	tuple[dict[str, Any], str]: The server document and its status string.
    """
    if isinstance(entry.get("server"), dict):
        server = entry["server"]
        official = (entry.get("_meta") or {}).get(_META_KEY) or {}
        return server, str(official.get("status") or "active")
    return entry, str(entry.get("status") or "active")


def dedupe_latest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the per-version registry feed to one entry per server.

    The unfiltered ``/v0.1/servers`` listing returns one row per *published version*,
    so a server shows up multiple times. Group by name and prefer the ``isLatest``
    version, falling back to the first seen when none is flagged. We never drop a
    server outright — if upstream metadata is missing/quirky (e.g. no row flagged
    latest), the server still appears. Insertion order is preserved.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        server, _ = _unwrap(entry)
        name = str(server.get("name") or "")
        if not name:
            continue
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = entry
            continue
        meta = (entry.get("_meta") or {}).get(_META_KEY) or {}
        existing_meta = (existing.get("_meta") or {}).get(_META_KEY) or {}
        # Upgrade to the isLatest row, but never downgrade away from one.
        if meta.get("isLatest") is True and existing_meta.get("isLatest") is not True:
            by_name[name] = entry
    return list(by_name.values())


def _list_item(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize a registry server entry for list responses.
    
    Parameters:
    	entry (dict[str, Any]): Raw registry entry document.
    
    Returns:
    	dict[str, Any]: Normalized list-item metadata for the server.
    """
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

    # A "deleted" server was removed from the registry (typically spam/malware/policy);
    # never offer it for install.
    if status == "deleted":
        installable = False

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
        "web_url": server.get("websiteUrl"),
    }


def to_detail(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Convert an official registry document into detail metadata and install drafts.
    
    Parameters:
    	entry (dict[str, Any]): Registry document to normalize.
    
    Returns:
    	dict[str, Any]: Normalized detail data containing server metadata, package drafts, and remotes.
    """
    server, status = _unwrap(entry)
    name = str(server.get("name") or "")
    version = server.get("version")
    packages = server.get("packages") or []
    remotes = server.get("remotes") or []

    drafts = [mapping.package_draft(i, pkg) for i, pkg in enumerate(packages) if isinstance(pkg, dict)]

    notes: list[str] = []
    if status == "deleted":
        # Removed from the registry (spam/malware/policy). Block install AND strip the
        # runnable command, so the review form can't be pre-filled with a launchable
        # spec for a moderation-removed package.
        reason = "This server was removed from the registry (status: deleted) — install is blocked."
        for d in drafts:
            d["installable"] = False
            d["reason"] = reason
            d["runner"] = None
            d["command"] = ""
            d["args"] = []
            d["env"] = {}
        notes.append(reason)

    return {
        "source": "official",
        "manual_install": False,
        "notes": notes,
        "server": {
            "name": name,
            "title": server.get("title") or name,
            "description": server.get("description") or "",
            "version": None if version in (None, "") else str(version),
            "status": status,
            "repository_url": (server.get("repository") or {}).get("url"),
            "web_url": server.get("websiteUrl"),
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
        """
        Fetch a page of registry servers.
        
        Parameters:
        	search (str | None): Text used to filter the server list.
        	cursor (str | None): Pagination cursor for the page to load.
        	limit (int | None): Maximum number of servers to return.
        
        Returns:
        	dict[str, Any]: A dictionary containing a normalized `servers` list and the next pagination cursor.
        """
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
        entries = dedupe_latest([e for e in servers if isinstance(e, dict)])
        result = {
            "servers": [_list_item(e) for e in entries],
            "next_cursor": metadata.get("nextCursor"),
        }
        self._cache.put(key, result)
        return result

    async def get_detail(
        self, http: httpx.AsyncClient, *, id: str, version: str
    ) -> dict[str, Any]:
        # Path params must be URL-encoded (e.g. ``io.x/name`` → ``io.x%2Fname``).
        """
        Fetch the registry detail document for a server version.
        
        Returns:
        	dict[str, Any]: The normalized detail record for the requested server version.
        """
        enc_name = quote(id, safe="")
        enc_version = quote(version or "latest", safe="")
        url = f"{BASE_URL}/v0.1/servers/{enc_name}/versions/{enc_version}"
        data = await base.get_json(http, url, {})
        if not isinstance(data, dict):
            raise base.CatalogUpstreamError("unexpected detail response from the MCP Registry")
        return to_detail(data)

    async def list_versions(self, http: httpx.AsyncClient, *, id: str) -> list[str]:
        """List a server's published versions, the registry's ``isLatest`` one first."""
        url = f"{BASE_URL}/v0.1/servers/{quote(id, safe='')}/versions"
        data = await base.get_json(http, url, {})
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, list):
            return []
        latest: list[str] = []
        rest: list[str] = []
        seen: set[str] = set()
        for entry in servers:
            if not isinstance(entry, dict):
                continue
            server, _ = _unwrap(entry)
            version = server.get("version")
            if not version:
                continue
            v_str = str(version)
            if v_str in seen:  # registry can return duplicate version rows
                continue
            seen.add(v_str)
            meta = (entry.get("_meta") or {}).get(_META_KEY) or {}
            (latest if meta.get("isLatest") else rest).append(v_str)
        return latest + rest
