"""Catalog API — browse upstream MCP directories and resolve install drafts.

Read-only and stateless: each call dispatches to a ``Source`` from the SSOT registry
(``catalog.registry``) and returns the normalized shapes. The API never special-cases a
source — adding a registry there makes it appear here automatically. Installing is *not*
a catalog endpoint: the SPA posts a resolved draft to ``POST /api/servers`` (with
``source="catalog:<id>"``), reusing the normal create + review path.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.api.schemas import CatalogDetail, CatalogList, CatalogSource, CatalogVersions
from app.catalog import registry
from app.catalog.base import CatalogUpstreamError, Source

router = APIRouter()


def _source(source_id: str) -> Source:
    """
    Resolve a catalog source by its identifier.
    
    Parameters:
    	source_id (str): The catalog source identifier.
    
    Returns:
    	Source: The resolved catalog source.
    
    Raises:
    	HTTPException: If the source identifier is unknown.
    """
    src = registry.get_source(source_id)
    if src is None:
        raise HTTPException(status_code=400, detail=f"unknown catalog source {source_id!r}")
    return src


@router.get("/catalog/sources", response_model=list[CatalogSource])
async def list_sources():
    """
    List the available catalog sources.
    
    Returns:
    	list[CatalogSource]: The configured catalog sources and their capabilities.
    """
    return registry.source_list()


@router.get("/catalog/servers", response_model=CatalogList)
async def list_catalog_servers(
    request: Request,
    source: str = Query(registry.DEFAULT_SOURCE),
    search: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=100),
):
    """
    List servers from a catalog source.
    
    Parameters:
    	source (str): Catalog source identifier.
    	search (str | None): Search term used to filter results.
    	cursor (str | None): Pagination cursor from a previous response.
    	limit (int | None): Maximum number of servers to return.
    
    Returns:
    	CatalogList: The servers for the selected source and the next pagination cursor, if any.
    """
    src = _source(source)
    try:
        data = await src.list_servers(
            request.app.state.http, search=search, cursor=cursor, limit=limit
        )
    except httpx.HTTPStatusError:
        # A list endpoint shouldn't 404, but guard anyway so a bad upstream status
        # surfaces as 502 rather than an uncaught 500 (mirrors get_catalog_server).
        raise HTTPException(status_code=502, detail=f"{src.label} directory unavailable")
    except CatalogUpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"{src.label} directory unavailable: {exc}")
    return CatalogList(source=source, servers=data["servers"], next_cursor=data["next_cursor"])


@router.get("/catalog/server/versions", response_model=CatalogVersions)
async def get_catalog_versions(
    request: Request,
    id: str = Query(..., description="the per-source server id/name from the list view"),
    source: str = Query(registry.DEFAULT_SOURCE),
):
    src = _source(source)
    try:
        versions = await src.list_versions(request.app.state.http, id=id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="server not found in catalog")
        raise HTTPException(status_code=502, detail=f"{src.label} directory unavailable")
    except CatalogUpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"{src.label} directory unavailable: {exc}")
    return CatalogVersions(versions=versions)


@router.get("/catalog/server", response_model=CatalogDetail)
async def get_catalog_server(
    request: Request,
    id: str = Query(..., description="the per-source server id/name from the list view"),
    source: str = Query(registry.DEFAULT_SOURCE),
    version: str = Query("latest"),
):
    """
    Fetch a catalog server detail by source-specific ID and version.
    
    Parameters:
    	id (str): The per-source server ID or name from the list view.
    	version (str): The catalog version to resolve.
    
    Returns:
    	CatalogDetail: The resolved server detail.
    
    Raises:
    	HTTPException: If the source is unknown, the server is not found, or the upstream directory is unavailable.
    """
    src = _source(source)
    try:
        return await src.get_detail(request.app.state.http, id=id, version=version)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="server not found in catalog")
        raise HTTPException(status_code=502, detail=f"{src.label} directory unavailable")
    except CatalogUpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"{src.label} directory unavailable: {exc}")
