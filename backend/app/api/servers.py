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
    ImportWarning,
    OAuthStatus,
    ServerClone,
    ServerCreate,
    ServerDetail,
    ServerSummary,
    ServerUpdate,
    Transports,
    Urls,
)
from app.auth import oauth_flow
from app.auth.oauth_store import ServerTokenStorage
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


def _oauth_signature(server: Server) -> tuple:
    """The OAuth-affecting config of a server. When this changes across a PATCH, the
    stored tokens (for the old upstream/provider/client) must be discarded."""
    return (
        bool(server.oauth),
        server.command,  # upstream URL — tokens are bound to this resource
        server.oauth_scopes or "",
        server.oauth_client_id,
        server.oauth_client_secret,
    )


def _oauth_status(server: Server) -> OAuthStatus:
    """OAuth auth state for a remote server (reads the shared token file)."""
    if not server.oauth:
        return OAuthStatus()
    st = ServerTokenStorage(server.id).status()
    return OAuthStatus(
        enabled=True,
        authenticated=st["authenticated"],
        needs_auth=not st["authenticated"],
        expires_at=st["expires_at"],
        has_refresh_token=st["has_refresh_token"],
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
        oauth=bool(server.oauth),
        oauth_scopes=server.oauth_scopes or "",
        oauth_client_id=server.oauth_client_id,
        oauth_has_client_secret=bool(server.oauth_client_secret),
        oauth_status=_oauth_status(server),
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
    provided = payload.model_dump()
    changes = {k: v for k, v in provided.items() if v is not None}
    # The generic None-drop implements partial PATCH, but it also swallows an *explicit*
    # null on the nullable OAuth client fields — so clearing a static client id/secret to
    # fall back to Dynamic Client Registration would be silently ignored. Preserve those two
    # when the client actually sent them (model_fields_set) so null means "clear".
    for key in ("oauth_client_id", "oauth_client_secret"):
        if key in payload.model_fields_set:
            changes[key] = provided[key]

    # Signature of the OAuth-relevant config before the edit, to decide token cleanup below.
    existing = repo.get_server(session, server_id)
    before = _oauth_signature(existing) if existing is not None else None

    try:
        server = service.update_server(session, server_id, changes)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sup = request.app.state.supervisor
    # If the OAuth config changed (upstream URL, scopes, client) or OAuth was turned off, the
    # stored tokens belong to the old provider/resource. Clear them so a restarted bridge can't
    # replay a stale credential and the status stops falsely reading "authenticated". Also
    # cancel any in-flight authorization (its background flow targets the OLD config) and, if
    # the server is enabled, restart the bridge so it drops the now-revoked in-memory token —
    # the client secret is excluded from config_hash, so a secret-only edit wouldn't otherwise
    # trigger a reconcile.
    oauth_changed = before is not None and before != _oauth_signature(server)
    if oauth_changed:
        oauth_flow.cancel_pending(server_id)
        # STOP the bridge before clearing: a running bridge's in-flight refresh could
        # otherwise set_tokens() after the clear and recreate credentials for the old
        # upstream/client, which the nudge below would then restart the server with.
        if server.enabled:
            await sup.stop(server_id)
        ServerTokenStorage(server_id).clear()

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
    # Cancel any in-flight authorization and drop stored upstream OAuth credentials for this
    # (now-deleted) server — otherwise a late callback could re-promote tokens and leave an
    # orphan credential file on disk for a server that no longer exists.
    oauth_flow.cancel_pending(server_id)
    ServerTokenStorage(server_id).clear()
    return Response(status_code=204)


def _oauth_callback_url(request: Request) -> str:
    """The public URL the upstream redirects the operator's browser back to after
    sign-in. Built from the same base as the copy-menu links so it matches the host
    the operator actually reached us on (and the operator-declared public URL when set).

    Behind an HTTPS-terminating reverse proxy without ``MCPE_PUBLIC_BASE_URL``, the ASGI
    request scheme is the plain ``http`` of the proxy→app hop; honor ``X-Forwarded-Proto``
    so the registered redirect URI is ``https`` (OAuth providers reject non-loopback http
    callbacks). The operator-declared public URL still wins when set."""
    base = _base_url(request)
    if not get_settings().public_base_url and base.startswith("http://"):
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded == "https":
            base = "https://" + base[len("http://"):]
    return f"{base}/api/oauth/callback"


@router.post("/servers/{server_id}/oauth/authorize")
async def start_oauth(server_id: str, request: Request, session: Session = Depends(get_session)):
    """Begin the interactive OAuth flow for a remote server: contact the upstream,
    register/discover, and return the provider authorization URL for the SPA to send
    the browser to. The operator finishes at ``/api/oauth/callback``."""
    server = repo.get_server(session, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    if server.runner != "remote" or not server.oauth:
        raise HTTPException(status_code=400, detail="this server does not use OAuth")
    try:
        url = await oauth_flow.begin_authorization(server, callback_url=_oauth_callback_url(request))
    except Exception as exc:  # discovery/registration failure or timeout
        raise HTTPException(status_code=502, detail=f"could not start OAuth: {exc}") from exc
    return {"authorize_url": url}


@router.post("/servers/{server_id}/oauth/disconnect", response_model=ServerDetail)
async def disconnect_oauth(
    server_id: str, request: Request, session: Session = Depends(get_session)
):
    """Forget the stored upstream tokens so the operator can re-authenticate from
    scratch (e.g. to switch accounts). A running bridge holds its access token in
    memory, so an enabled server is restarted immediately: it re-reads the now-empty
    store and stops serving with the revoked credential until re-authenticated."""
    server = repo.get_server(session, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    # Only a remote OAuth server has anything to disconnect; reject others so a stale UI
    # action or stray API call can't bounce an unrelated running server.
    if server.runner != "remote" or not server.oauth:
        raise HTTPException(status_code=400, detail="this server does not use OAuth")
    sup = request.app.state.supervisor
    # Cancel any in-flight authorization first: otherwise a callback for that parked flow
    # could land after the clear and re-promote tokens, silently re-authenticating the
    # server the operator just disconnected.
    oauth_flow.cancel_pending(server_id)
    # STOP the bridge before clearing: a running bridge may have an in-flight refresh whose
    # set_tokens() would otherwise recreate the file after clear(), and the nudge would then
    # restart the server with fresh credentials despite the UI reporting it disconnected.
    if server.enabled:
        await sup.stop(server_id)
    ServerTokenStorage(server_id).clear()
    # The server stays enabled, so the reconciler restarts it; with no tokens it can't
    # authenticate upstream and surfaces as needing re-auth — the intended "disconnected"
    # state (matches the connect path, which also restarts to pick up fresh tokens).
    if server.enabled:
        sup.nudge()
    return _detail(server, sup, session, _base_url(request))


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
        created, skipped, warnings = service.import_mcp_servers(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    base = _base_url(request)
    return ImportResult(
        created=[_summary(s, sup, session, base) for s in created],
        skipped=[ImportSkipped(**s) for s in skipped],
        warnings=[ImportWarning(**w) for w in warnings],
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
