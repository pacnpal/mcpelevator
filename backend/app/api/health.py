"""Health endpoints.

``/health`` is control-plane liveness (the SPA polls it for its status dot).
``/health/{slug}`` and ``/health/summary`` report per-server readiness so a load
balancer or client can check whether a specific proxied MCP server is accepting
requests before sending traffic.

Only ``/health`` stays public (outside the bearer-gated routers). The
inventory-bearing per-server health routes use the control-plane bearer gate when
enforcement is on. The Host/Origin allowlist middleware still guards every
``/api`` path. The per-server responses are deliberately COARSE — only a
pass/fail ``running`` signal, never ``state``/``last_error``/inventory detail.
Surfacing raw error text or lifecycle state here would turn a readiness probe
into an unauthenticated diagnostics endpoint; detailed state lives behind the
gated control plane (``/api/servers``) instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app import __version__
from app.auth.control_plane import require_control_plane
from app.db import get_session, repo

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/health/summary", dependencies=[Depends(require_control_plane)])
async def health_summary(request: Request, session: Session = Depends(get_session)) -> dict:
    """Pass/fail readiness for every server, plus an overall flag. ``ok`` is true
    when no *enabled* server is failing to run — disabled servers are intentionally
    down and don't count against it. Readiness comes from the in-memory supervisor
    (a live unit with an allocated port), so this touches no per-server DB rows.

    Each entry is just ``{slug, running}`` — ``enabled`` is read to decide the
    overall status but is not exposed, and no state/error detail is leaked."""
    sup = request.app.state.supervisor
    servers = []
    ok = True
    for s in repo.list_servers(session):
        running = sup.endpoint(s.slug) is not None
        servers.append({"slug": s.slug, "running": running})
        if s.enabled and not running:
            ok = False
    return {"status": "ok" if ok else "degraded", "servers": servers}


@router.get("/health/{slug}", response_model=None, dependencies=[Depends(require_control_plane)])
async def health_slug(
    slug: str, request: Request, session: Session = Depends(get_session)
) -> dict | JSONResponse:
    """Per-server readiness. 404 if the slug is unknown; 503 when the server exists
    but isn't currently accepting connections (so a balancer gets a failing status
    code, not just a 200 body it has to parse). The 503 body is the same flat,
    coarse shape as the 200 — ``{slug, running, status}`` — so a client parses one
    schema either way, and no privileged state/error detail is exposed.

    (``summary`` can never reach here: it's a reserved slug, so the static
    ``/health/summary`` route can't be shadowed by a server named "summary".)"""
    server = repo.get_server_by_slug(session, slug)
    if server is None:
        raise HTTPException(status_code=404, detail="unknown server")
    running = request.app.state.supervisor.endpoint(server.slug) is not None
    body = {"slug": server.slug, "running": running, "status": "ok" if running else "unavailable"}
    if not running:
        return JSONResponse(status_code=503, content=body)
    return body
