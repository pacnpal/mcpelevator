"""Health endpoints.

``/health`` is control-plane liveness (the SPA polls it for its status dot).
``/health/{slug}`` and ``/health/summary`` report per-server readiness so a load
balancer or client can check whether a specific proxied MCP server is accepting
requests before sending traffic — without listing the full control-plane state.

Like ``/health``, these stay public (outside the bearer-gated routers); the
Host/Origin allowlist middleware still guards every ``/api`` path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app import __version__
from app.db import get_session, repo
from app.db.models import Server, ServerRuntime

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


def _server_health(
    server: Server, sup, session: Session, runtimes: dict[str, ServerRuntime] | None = None
) -> dict:
    """Readiness for one server. ``running`` is the operational signal a balancer
    cares about: a unit that is up with an allocated port (so the proxy has a live
    backend to forward to). State/last_error are included for diagnostics.

    A live unit is the source of truth; the persisted runtime row is only consulted
    as a fallback. ``runtimes`` lets the summary endpoint pass a pre-fetched map so it
    reads every fallback row in one query instead of one-per-server (avoids N+1)."""
    running = sup.endpoint(server.slug) is not None
    unit = sup.unit(server.id)
    if unit is not None:
        state, last_error = unit.state, unit.last_error
    else:
        runtime = runtimes.get(server.id) if runtimes is not None else repo.get_runtime(session, server.id)
        state = runtime.state if runtime is not None else "stopped"
        last_error = runtime.last_error if runtime is not None else None
    return {
        "slug": server.slug,
        "enabled": server.enabled,
        "running": running,
        "state": state,
        "last_error": last_error,
    }


@router.get("/health/summary")
async def health_summary(request: Request, session: Session = Depends(get_session)) -> dict:
    """Pass/fail readiness for every server, plus an overall flag. ``ok`` is true
    when no *enabled* server is failing to run — disabled servers are intentionally
    down and don't count against it."""
    sup = request.app.state.supervisor
    runtimes = {r.server_id: r for r in session.exec(select(ServerRuntime)).all()}
    servers = [_server_health(s, sup, session, runtimes) for s in repo.list_servers(session)]
    ok = all(h["running"] for h in servers if h["enabled"])
    return {"status": "ok" if ok else "degraded", "servers": servers}


@router.get("/health/{slug}", response_model=None)
async def health_slug(
    slug: str, request: Request, session: Session = Depends(get_session)
) -> dict | JSONResponse:
    """Per-server readiness. 404 if the slug is unknown; 503 when the server exists
    but isn't currently accepting connections (so a balancer gets a failing status
    code, not just a 200 body it has to parse). The 503 body is the same flat shape
    as the 200 — not nested under ``detail`` — so a client parses one schema either way.

    (``summary`` can never reach here: it's a reserved slug, so the static
    ``/health/summary`` route can't be shadowed by a server named "summary".)"""
    server = repo.get_server_by_slug(session, slug)
    if server is None:
        raise HTTPException(status_code=404, detail="unknown server")
    health = _server_health(server, request.app.state.supervisor, session)
    if not health["running"]:
        return JSONResponse(status_code=503, content=health)
    return health
