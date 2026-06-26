"""Server control-plane CRUD + lifecycle.

Handlers only read freely and WRITE DESIRED STATE — they never spawn/kill. They
``nudge()`` the supervisor, which reconciles asynchronously. Live state for the
response is read from the supervisor unit (current), falling back to the persisted
runtime row, then ``stopped``.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlmodel import Session
from starlette.responses import Response

from app.api.schemas import (
    ImportResult,
    ImportSkipped,
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

router = APIRouter()


def _live_state(server: Server, sup, session: Session):
    unit = sup.unit(server.id)
    if unit is not None:
        return unit.state, unit.last_error, unit.pid, unit.port, unit.tools
    runtime = repo.get_runtime(session, server.id)
    if runtime is not None:
        return runtime.state, runtime.last_error, runtime.pid, runtime.port, runtime.tools
    return "stopped", None, None, None, []


def _summary(server: Server, sup, session: Session) -> ServerSummary:
    state, last_error, pid, port, tools = _live_state(server, sup, session)
    base = get_settings().base_url
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
        last_error=last_error,
        pid=pid,
        port=port,
        tools_count=len(tools or []),
    )


def _detail(server: Server, sup, session: Session) -> ServerDetail:
    summary = _summary(server, sup, session)
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
    return [_summary(s, sup, session) for s in repo.list_servers(session)]


@router.post("/servers", response_model=ServerSummary, status_code=201)
async def create_server(
    payload: ServerCreate, request: Request, session: Session = Depends(get_session)
):
    try:
        server = service.create_server(session, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    if server.enabled:
        sup.nudge()
    return _summary(server, sup, session)


@router.get("/servers/{server_id}", response_model=ServerDetail)
async def get_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    server = repo.get_server(session, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return _detail(server, request.app.state.supervisor, session)


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
    sup.nudge()  # config_hash may have changed -> reconciler restarts if needed
    return _summary(server, sup, session)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    sup = request.app.state.supervisor
    await sup.stop(server_id)  # tear down the process before removing desired state
    if not repo.delete_server(session, server_id):
        raise HTTPException(status_code=404, detail="server not found")
    return Response(status_code=204)


@router.post("/servers/{server_id}/enable", response_model=ServerSummary)
async def enable_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        server = service.set_enabled(session, server_id, True)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    sup = request.app.state.supervisor
    sup.nudge()
    return _summary(server, sup, session)


@router.post("/servers/{server_id}/disable", response_model=ServerSummary)
async def disable_server(server_id: str, request: Request, session: Session = Depends(get_session)):
    try:
        server = service.set_enabled(session, server_id, False)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    sup = request.app.state.supervisor
    sup.nudge()
    return _summary(server, sup, session)


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
    return ImportResult(
        created=[_summary(s, sup, session) for s in created],
        skipped=[ImportSkipped(**s) for s in skipped],
    )
