"""Server control-plane CRUD + lifecycle.

Handlers only read freely and WRITE DESIRED STATE — they never spawn/kill. They
``nudge()`` the supervisor, which reconciles asynchronously. Live state for the
response is read from the supervisor unit (current), falling back to the persisted
runtime row, then ``stopped``.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlmodel import Session
from starlette.responses import Response, StreamingResponse

from app.api.schemas import (
    ImportResult,
    ImportSkipped,
    ServerClone,
    ServerCreate,
    ServerDetail,
    ServerSummary,
    ServerUpdate,
    Transports,
    Urls,
)
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Server
from app.registry import service
from app.registry import settings as runtime_settings

router = APIRouter()


def _live_state(server: Server, sup, session: Session):
    unit = sup.unit(server.id)
    if unit is not None:
        return unit.state, unit.last_error, unit.pid, unit.port, unit.tools
    runtime = repo.get_runtime(session, server.id)
    if runtime is not None:
        return runtime.state, runtime.last_error, runtime.pid, runtime.port, runtime.tools
    return "stopped", None, None, None, []


def _base_url(request: Request) -> str:
    """Base URL for the copy-menu server links. Prefer the operator-declared public URL;
    otherwise use the host the client actually reached us on — so a LAN device (with
    ``allow_private_lan``) copies ``http://192.168.1.50:8080/...`` rather than the
    ``0.0.0.0``→``127.0.0.1`` rewrite baked into ``settings.base_url``. The Host header is
    already validated by the control-plane allowlist before any handler runs, so it's a
    trusted value here. Falls back to the derived settings URL when there's no Host."""
    settings = get_settings()
    if settings.public_base_url:
        return settings.base_url  # operator-declared canonical URL wins
    host = request.headers.get("host", "").strip()
    if host:
        return f"{request.url.scheme or 'http'}://{host}"
    return settings.base_url


def _summary(server: Server, sup, session: Session, base: str) -> ServerSummary:
    state, last_error, pid, port, tools = _live_state(server, sup, session)
    auth = server.auth_provider
    if auth == "inherit":
        auth = runtime_settings.default_auth_provider(session)
    # Legacy DBs (the old schema accepted any string) may hold e.g. "Bearer"; coerce
    # to the response model's Literal so a single bad row can't 500 the whole dashboard.
    auth = (auth or "").strip().lower()
    if auth not in ("none", "bearer"):
        auth = "none"
    return ServerSummary(
        id=server.id,
        slug=server.slug,
        name=server.name,
        runner=server.runner,
        enabled=server.enabled,
        state=state,
        transports=Transports(mcp_http=server.mcp_http, rest_openapi=server.rest_openapi),
        urls=Urls(
            mcp=f"{base}/s/{server.slug}/mcp" if server.mcp_http else None,
            rest=f"{base}/s/{server.slug}/rest" if server.rest_openapi else None,
        ),
        auth=auth,
        last_error=last_error,
        pid=pid,
        port=port,
        tools_count=len(tools or []),
    )


def _detail(server: Server, sup, session: Session, base: str) -> ServerDetail:
    summary = _summary(server, sup, session, base)
    _, _, _, _, tools = _live_state(server, sup, session)
    return ServerDetail(
        **summary.model_dump(),
        command=server.command,
        args=server.args,
        env=server.env,
        cwd=server.cwd,
        auth_provider=server.auth_provider,
        config_hash=server.config_hash,
        source=server.source,
        tools=tools or [],
    )


@router.get("/servers", response_model=list[ServerSummary])
async def list_servers(request: Request, session: Session = Depends(get_session)):
    sup = request.app.state.supervisor
    base = _base_url(request)
    return [_summary(s, sup, session, base) for s in repo.list_servers(session)]


@router.post("/servers", response_model=ServerSummary, status_code=201)
async def create_server(
    payload: ServerCreate, request: Request, session: Session = Depends(get_session)
):
    """
    Create a new server in the desired state.
    
    Parameters:
        payload (ServerCreate): Server configuration to persist.
        request (Request): Incoming request used to access the supervisor and base URL.
        session (Session): Database session.
    
    Returns:
        ServerSummary: The created server summary.
    """
    fields = payload.model_dump()
    # Provenance: only a "catalog:<id>" value is trusted (a registry install); any other
    # client-supplied string falls back to the service default ("manual"). Cap length so a
    # giant value can't bloat the row.
    raw_source = fields.pop("source", None)
    if isinstance(raw_source, str) and raw_source.startswith("catalog:"):
        fields["source"] = raw_source[:200]
    else:
        fields["source"] = "manual"
    try:
        server = service.create_server(session, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    if server.enabled:
        sup.nudge()
    return _summary(server, sup, session, _base_url(request))


@router.get("/servers/{server_id}", response_model=ServerDetail)
async def get_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    server = repo.get_server(session, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return _detail(server, request.app.state.supervisor, session, _base_url(request))


@router.patch("/servers/{server_id}", response_model=ServerSummary)
async def update_server(
    server_id: str,
    payload: ServerUpdate,
    request: Request,
    session: Session = Depends(get_session),
):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        server = service.update_server(session, server_id, changes)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    if "slug" in changes:
        # Re-point a running unit's proxy routing without a restart (config_hash
        # excludes slug, so the reconciler won't do it).
        sup.rename_slug(server_id, server.slug)
    sup.nudge()  # config_hash may have changed -> reconciler restarts if needed
    return _summary(server, sup, session, _base_url(request))


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    sup = request.app.state.supervisor
    await sup.stop(server_id)  # tear down the process before removing desired state
    if not repo.delete_server(session, server_id):
        raise HTTPException(status_code=404, detail="server not found")
    return Response(status_code=204)


@router.post("/servers/{server_id}/clone", response_model=ServerSummary, status_code=201)
async def clone_server(
    server_id: str,
    request: Request,
    payload: ServerClone = Body(default_factory=ServerClone),
    session: Session = Depends(get_session),
):
    """Duplicate a server's config into a new, disabled server (a fresh id + unique
    slug). The operator reviews/edits the copy, then enables it."""
    try:
        server = service.clone_server(session, server_id, name=payload.name)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Clone is created disabled; nothing to reconcile until the user enables it.
    return _summary(server, request.app.state.supervisor, session, _base_url(request))


@router.post("/servers/{server_id}/enable", response_model=ServerSummary)
async def enable_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        server = service.set_enabled(session, server_id, True)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        # e.g. enabling a docker server while the (root-equivalent) docker runner is off.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sup = request.app.state.supervisor
    sup.nudge()
    return _summary(server, sup, session, _base_url(request))


@router.post("/servers/{server_id}/disable", response_model=ServerSummary)
async def disable_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        server = service.set_enabled(session, server_id, False)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    sup = request.app.state.supervisor
    sup.nudge()
    return _summary(server, sup, session, _base_url(request))


@router.post("/servers/import", response_model=ImportResult, status_code=201)
async def import_servers(
    request: Request,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
):
    """Bulk-create from a standard mcpServers JSON config. Imported servers are
    disabled by default — the user reviews, then enables."""
    try:
        created, skipped = service.import_mcp_servers(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    base = _base_url(request)
    return ImportResult(
        created=[_summary(s, sup, session, base) for s in created],
        skipped=[ImportSkipped(**s) for s in skipped],
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.get("/servers/{server_id}/logs")
async def stream_logs(server_id: str, request: Request, session: Session = Depends(get_session)):
    """SSE stream of a server's live bridge logs.

    Replays the in-memory backlog, then tails new lines until the client
    disconnects or the server stops (a server's LogBuffer lives only while its
    unit is running). 404 if the server row doesn't exist.
    """
    if repo.get_server(session, server_id) is None:
        raise HTTPException(status_code=404, detail="server not found")

    sup = request.app.state.supervisor
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # don't let any intermediary buffer the stream
    }

    async def events():
        unit = sup.unit(server_id)
        if unit is None:
            yield _sse({"type": "info", "message": "server not running"})
            return
        # Subscribe BEFORE snapshotting, with no await between: append() runs on
        # this same event loop, so no line can slip in to be missed or duplicated.
        queue = unit.logs.subscribe()
        backlog = unit.logs.snapshot()
        try:
            for line in backlog:
                yield _sse({"line": line})
            while True:
                if await request.is_disconnected() or sup.unit(server_id) is not unit:
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse({"line": line})
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # heartbeat keeps the connection alive
        finally:
            unit.logs.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
