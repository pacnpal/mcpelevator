"""Server control-plane CRUD + lifecycle.

Handlers only read freely and WRITE DESIRED STATE — they never spawn/kill. They
``nudge()`` the supervisor, which reconciles asynchronously. Live state for the
response is read from the supervisor unit (current), falling back to the persisted
runtime row, then ``stopped``.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from datetime import timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastmcp import Client
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
    StartupStatus,
    ToolCallRequest,
    ToolCallResult,
    Transports,
    Urls,
)
from app.api.util import base_url, resync_groups
from app.auth import oauth_flow, policy
from app.auth.principal import Principal, current_principal
from app.auth.oauth_store import ServerTokenStorage
from app.config import get_settings
from app.db import get_session, repo
from app.db.models import Server
from app.groups import registry as group_registry
from app.registry import service
from app.registry import settings as runtime_settings

router = APIRouter()


def _queued_status(server: Server, started_at=None) -> StartupStatus:
    started_at = started_at or server.updated_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return StartupStatus(
        phase="queued",
        attempt=1,
        max_attempts=get_settings().restart_budget,
        activation_started_at=started_at,
    )


def _live_state(server: Server, sup, session: Session):
    unit = sup.unit(server.id)
    requested_at = sup.activation_requested_at(server.id)
    runtime = repo.get_runtime(session, server.id)
    if server.enabled:
        if requested_at is not None:
            return "starting", None, None, None, [], _queued_status(server, requested_at)
        if unit is not None and (
            unit.config_hash != server.config_hash or unit.state in ("stopped", "stopping")
        ):
            return "starting", None, None, None, [], _queued_status(server)
        if unit is None:
            # "idle" is a deliberate quiescence, not a startup in progress: surface it
            # as-is (with the cached tool list) instead of the queued/starting shape.
            if runtime is not None and runtime.state in ("failed", "unhealthy", "idle"):
                return runtime.state, runtime.last_error, None, None, runtime.tools, None
            started_at = runtime.updated_at if runtime is not None else server.updated_at
            return "starting", None, None, None, [], _queued_status(server, started_at)
    else:
        if unit is not None and unit.state != "stopped":
            return "stopping", None, None, None, [], None
        if runtime is not None and runtime.state != "stopped":
            return "stopping", None, None, None, [], None
    if unit is not None:
        status = StartupStatus(**vars(unit.startup_status)) if unit.startup_status else None
        return unit.state, unit.last_error, unit.pid, unit.port, unit.tools, status
    if runtime is not None:
        return runtime.state, runtime.last_error, runtime.pid, runtime.port, runtime.tools, None
    return "stopped", None, None, None, [], None




def _owner_name(session: Session, owner_id: str | None) -> str | None:
    if owner_id is None:
        return None
    user = repo.get_user(session, owner_id)
    return user.name if user is not None else None


def _visible(
    principal: Principal, session: Session, server_id: str
) -> Server:
    """Resolve + authorize a /servers/{id}-shaped route in one step: a server the
    principal can't see 404s exactly like one that doesn't exist (policy module)."""
    return policy.require_visible_server(principal, repo.get_server(session, server_id))


def _summary(server: Server, sup, session: Session, base: str, live=None) -> ServerSummary:
    state, last_error, pid, port, tools, startup_status = live or _live_state(
        server, sup, session
    )
    auth = server.auth_provider
    if auth == "inherit":
        auth = runtime_settings.default_auth_provider(session)
    # Legacy DBs (the old schema accepted any string) may hold e.g. "Bearer"; coerce
    # to the response model's Literal so a single bad row can't 500 the whole dashboard.
    auth = (auth or "").strip().lower()
    if auth not in ("none", "bearer", "oauth"):
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
        startup_status=startup_status,
        owner_id=server.owner_id,
        owner_name=_owner_name(session, server.owner_id),
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
    live = _live_state(server, sup, session)
    summary = _summary(server, sup, session, base, live)
    tools = live[4]
    return ServerDetail(
        **summary.model_dump(),
        command=server.command,
        args=server.args,
        run_args=server.run_args or [],
        env=server.env,
        cwd=server.cwd,
        setup_script=server.setup_script or "",
        auth_provider=server.auth_provider,
        oauth=bool(server.oauth),
        oauth_scopes=server.oauth_scopes or "",
        oauth_client_id=server.oauth_client_id,
        oauth_has_client_secret=bool(server.oauth_client_secret),
        oauth_status=_oauth_status(server),
        idle_timeout_s=server.idle_timeout_s,
        config_hash=server.config_hash,
        source=server.source,
        tools=tools or [],
    )


@router.get("/servers", response_model=list[ServerSummary])
async def list_servers(
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    sup = request.app.state.supervisor
    base = base_url(request)
    servers = policy.visible_servers(principal, repo.list_servers(session))
    return [_summary(s, sup, session, base) for s in servers]


@router.post("/servers", response_model=ServerSummary, status_code=201)
async def create_server(
    payload: ServerCreate,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
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
    # Multi-user: runner permission is checked BEFORE anything persists, and the
    # creator becomes the owner (synthetic admins have no user_id -> admin-owned).
    policy.require_runner_allowed(principal, fields.get("runner") or "npx")
    fields["owner_id"] = principal.user_id
    try:
        # Threadpool: the config write derives config_hash with scrypt (memory-hard by
        # design) — keep that off the event loop so /s proxy traffic isn't stalled.
        server = await run_in_threadpool(service.create_server, session, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    if server.enabled:
        sup.request_activation(server.id)
    return _summary(server, sup, session, base_url(request))


@router.get("/servers/{server_id}", response_model=ServerDetail)
async def get_server(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    server = _visible(principal, session, server_id)
    return _detail(server, request.app.state.supervisor, session, base_url(request))


@router.patch("/servers/{server_id}", response_model=ServerSummary)
async def update_server(
    server_id: str,
    payload: ServerUpdate,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    existing_row = _visible(principal, session, server_id)
    provided = payload.model_dump()
    changes = {k: v for k, v in provided.items() if v is not None}

    # Ownership reassignment is admin-only and handled OUTSIDE the config write
    # (owner is identity, not launch config): pop it here, apply after the PATCH.
    owner_change_requested = "owner_id" in payload.model_fields_set
    new_owner = changes.pop("owner_id", None)
    if owner_change_requested:
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="only admins may reassign owners")
        if new_owner is not None and repo.get_user(session, new_owner) is None:
            raise HTTPException(status_code=400, detail=f"unknown user {new_owner!r}")

    # Runner policy on edit: a member without local-runner permission may still
    # start/stop a local server an admin provisioned for them, but must not shape
    # WHAT it executes — changing anything launch-affecting on (or converting a
    # server into) a local runner requires the permission.
    target_runner = changes.get("runner", existing_row.runner)
    launch_fields = {"runner", "command", "args", "run_args", "env", "cwd", "setup_script"}
    if changes.keys() & launch_fields:
        policy.require_runner_allowed(principal, target_runner)
    # The generic None-drop implements partial PATCH, but it also swallows an *explicit*
    # null on the nullable OAuth client fields — so clearing a static client id/secret to
    # fall back to Dynamic Client Registration would be silently ignored. Preserve those
    # when the client actually sent them (model_fields_set) so null means "clear" (and,
    # for idle_timeout_s, "inherit the global default").
    for key in ("oauth_client_id", "oauth_client_secret", "idle_timeout_s"):
        if key in payload.model_fields_set:
            changes[key] = provided[key]

    # Signature of the OAuth-relevant config before the edit, to decide token cleanup below.
    existing = repo.get_server(session, server_id)
    before = _oauth_signature(existing) if existing is not None else None
    before_hash = existing.config_hash if existing is not None else None
    auth_changed = "auth_provider" in changes
    sup = request.app.state.supervisor

    try:
        # Threadpool: see create_server — the recomputed config_hash is a scrypt derivation.
        transition = (
            request.app.state.groups.auth_transition() if auth_changed else nullcontext()
        )
        async with transition:
            server = await run_in_threadpool(service.update_server, session, server_id, changes)
            # Remove the old endpoint before an auth transition can publish group
            # routes under the new policy. _stop pops the endpoint before waiting for
            # process teardown, so no request can enter with stale credentials.
            oauth_changed = before is not None and before != _oauth_signature(server)
            if oauth_changed:
                oauth_flow.cancel_pending(server_id)
                await sup.stop(server_id)
                ServerTokenStorage(server_id).clear()

            if "slug" in changes:
                # Re-point a running unit's proxy routing without a restart (config_hash
                # excludes slug, so the reconciler won't do it).
                sup.rename_slug(server_id, server.slug)
            if server.enabled and before_hash != server.config_hash:
                sup.request_activation(server_id)
            else:
                sup.nudge()
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if auth_changed:
            await resync_groups(request)

    if not auth_changed and changes.keys() & {"mcp_http", "slug"}:
        await resync_groups(request)  # membership/namespace changed — no async gap
    if owner_change_requested:
        # After the config write succeeded: owner is identity, applied via its own
        # narrow writer (no updated_at bump, no config_hash change, no bridge bounce).
        old_owner = existing_row.owner_id
        repo.set_owner(session, server_id, new_owner)
        server.owner_id = new_owner
        if old_owner is not None and old_owner != new_owner:
            # Reassigning revokes the FORMER owner's data-plane tokens for this
            # server: they can no longer see or manage it, so a token they minted
            # must not keep authorizing its /s endpoint. Tokens minted by admins
            # (user_id NULL or another user) are untouched — those are deliberate
            # grants, not the former owner's self-service.
            stale = [
                t.id
                for t in repo.list_tokens(session)
                if t.user_id == old_owner and t.scope == server_id
            ]
            repo.delete_tokens_by_ids(session, stale)
    return _summary(server, sup, session, base_url(request))


def _prune_then_delete(session: Session, server_id: str) -> bool:
    """Prune the server from every group's explicit member list, then delete the row —
    both under a SINGLE hold of the config write lock. Run in the threadpool by the
    caller, never on the event loop: prune_server and service.delete_server each take
    that lock, and a bulk import holding it while deriving scrypt hashes would otherwise
    stall the loop (and all proxy/API traffic) here.

    Prune FIRST so an interruption between the two commits leaves a benign state — the
    row lingers but no group references a missing id (which validate_at_startup would
    refuse to boot on), and a retried delete completes it. Holding the lock across BOTH
    also closes the window where a concurrent group write could re-add this server after
    the prune but before the delete. The lock is reentrant, so the inner acquisitions in
    prune_server / delete_server are cheap no-ops. A no-op prune for a nonexistent id.
    Returns False when the server didn't exist (-> 404)."""
    with service.config_write_lock():
        group_registry.prune_server(session, server_id)
        return service.delete_server(session, server_id)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    _visible(principal, session, server_id)
    sup = request.app.state.supervisor
    sup.cancel_activation_request(server_id)
    await sup.stop(server_id)  # tear down the process before removing desired state
    # Prune + delete in the worker thread (see _prune_then_delete): both take the config
    # write lock, so the wait must not sit on the event loop.
    if not await run_in_threadpool(_prune_then_delete, session, server_id):
        raise HTTPException(status_code=404, detail="server not found")
    # Cancel any in-flight authorization and drop stored upstream OAuth credentials for this
    # (now-deleted) server — otherwise a late callback could re-promote tokens and leave an
    # orphan credential file on disk for a server that no longer exists.
    oauth_flow.cancel_pending(server_id)
    ServerTokenStorage(server_id).clear()
    await resync_groups(request)
    return Response(status_code=204)


def _oauth_callback_url(request: Request) -> str:
    """The public URL the upstream redirects the operator's browser back to after
    sign-in. Built from the same base as the copy-menu links so it matches the host
    the operator actually reached us on (and the operator-declared public URL when set).

    Behind an HTTPS-terminating reverse proxy without ``MCPE_PUBLIC_BASE_URL``, the ASGI
    request scheme is the plain ``http`` of the proxy→app hop; honor ``X-Forwarded-Proto``
    so the registered redirect URI is ``https`` (OAuth providers reject non-loopback http
    callbacks). The operator-declared public URL still wins when set."""
    base = base_url(request)
    if not get_settings().public_base_url and base.startswith("http://"):
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded == "https":
            base = "https://" + base[len("http://"):]
    return f"{base}/api/oauth/callback"


@router.post("/servers/{server_id}/oauth/authorize")
async def start_oauth(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """Begin the interactive OAuth flow for a remote server: contact the upstream,
    register/discover, and return the provider authorization URL for the SPA to send
    the browser to. The operator finishes at ``/api/oauth/callback``."""
    server = _visible(principal, session, server_id)
    if server.runner != "remote" or not server.oauth:
        raise HTTPException(status_code=400, detail="this server does not use OAuth")
    try:
        url = await oauth_flow.begin_authorization(server, callback_url=_oauth_callback_url(request))
    except oauth_flow.OAuthBeginError as exc:
        # Already an operator-facing message with the right status (e.g. a 429 when the
        # upstream rate-limits client registration) — surface it verbatim.
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:  # discovery/registration failure or timeout
        raise HTTPException(status_code=502, detail=f"could not start OAuth: {exc}") from exc
    return {"authorize_url": url}


@router.post("/servers/{server_id}/oauth/disconnect", response_model=ServerDetail)
async def disconnect_oauth(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """Forget the stored upstream tokens so the operator can re-authenticate from
    scratch (e.g. to switch accounts). A running bridge holds its access token in
    memory, so an enabled server is restarted immediately: it re-reads the now-empty
    store and stops serving with the revoked credential until re-authenticated."""
    server = _visible(principal, session, server_id)
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
    # Unconditional — sup.stop is idempotent, and gating on ``enabled`` would miss a bridge
    # still winding down from a just-toggled row.
    await sup.stop(server_id)
    ServerTokenStorage(server_id).clear()
    # The server stays enabled, so the reconciler restarts it; with no tokens it can't
    # authenticate upstream and surfaces as needing re-auth — the intended "disconnected"
    # state (matches the connect path, which also restarts to pick up fresh tokens).
    if server.enabled:
        sup.request_activation(server_id)
    return _detail(server, sup, session, base_url(request))


@router.post("/servers/{server_id}/clone", response_model=ServerSummary, status_code=201)
async def clone_server(
    server_id: str,
    request: Request,
    payload: ServerClone = Body(default_factory=ServerClone),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """Duplicate a server's config into a new, disabled server (a fresh id + unique
    slug). The operator reviews/edits the copy, then enables it."""
    src = _visible(principal, session, server_id)
    # Cloning IS creating: the same runner policy applies, and the cloner owns the copy.
    policy.require_runner_allowed(principal, src.runner)
    try:
        # Threadpool: see create_server — the clone's config_hash is a scrypt derivation.
        server = await run_in_threadpool(
            service.clone_server, session, server_id,
            name=payload.name, owner_id=principal.user_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Clone is created disabled; nothing to reconcile until the user enables it.
    return _summary(server, request.app.state.supervisor, session, base_url(request))


@router.post("/servers/{server_id}/enable", response_model=ServerSummary)
async def enable_server(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    existing = _visible(principal, session, server_id)
    was_enabled = bool(existing.enabled)
    try:
        # Threadpool: set_enabled takes the registry write lock, and an import in a worker
        # can hold that lock across many derivations — don't wait for it on the event loop.
        server = await run_in_threadpool(service.set_enabled, session, server_id, True)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    except ValueError as exc:
        # e.g. enabling a docker server while the (root-equivalent) docker runner is off.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sup = request.app.state.supervisor
    if not was_enabled:
        sup.request_activation(server_id)
    else:
        sup.nudge()
    return _summary(server, sup, session, base_url(request))


@router.post("/servers/{server_id}/disable", response_model=ServerSummary)
async def disable_server(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    _visible(principal, session, server_id)
    try:
        # Threadpool: see enable_server — the lock wait must not block the loop.
        server = await run_in_threadpool(service.set_enabled, session, server_id, False)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found")
    sup = request.app.state.supervisor
    sup.cancel_activation_request(server_id)
    sup.nudge()
    return _summary(server, sup, session, base_url(request))


@router.post("/servers/{server_id}/retry", response_model=ServerSummary)
async def retry_server(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    server = _visible(principal, session, server_id)
    if not server.enabled:
        raise HTTPException(status_code=409, detail="disabled servers cannot be retried")
    sup = request.app.state.supervisor
    state, _, _, _, _, startup_status = _live_state(server, sup, session)
    if startup_status is not None or state not in ("failed", "unhealthy"):
        raise HTTPException(status_code=409, detail="server is not in a retryable state")
    if not await sup.retry(server_id):
        raise HTTPException(status_code=409, detail="server is no longer retryable")
    return _summary(server, sup, session, base_url(request))


@router.post("/servers/import", response_model=ImportResult, status_code=201)
async def import_servers(
    request: Request,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """Bulk-create from a standard mcpServers JSON config. Imported servers are
    disabled by default — the user reviews, then enables. The importer owns every
    created row; local-runner entries a restricted member may not create land in
    ``skipped`` (per entry), so their remote entries still import."""
    try:
        # Threadpool: a bulk import derives one scrypt config_hash per server — N stacked
        # derivations must not sit on the event loop.
        created, skipped, warnings = await run_in_threadpool(
            lambda: service.import_mcp_servers(
                session,
                payload,
                owner_id=principal.user_id,
                allow_local=policy.can_use_runner(principal, "npx"),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sup = request.app.state.supervisor
    base = base_url(request)
    return ImportResult(
        created=[_summary(s, sup, session, base) for s in created],
        skipped=[ImportSkipped(**s) for s in skipped],
        warnings=[ImportWarning(**w) for w in warnings],
    )


async def _call_bridge_tool(url: str, name: str, arguments: dict, timeout: float):
    """One playground invocation against a bridge's loopback MCP endpoint.

    A fresh short-lived FastMCP client session per call — matching the bridge's own
    fresh-upstream-session-per-request proxy semantics, so a playground call behaves
    exactly like a real client's. ``raise_on_error=False`` keeps a tool's own failure
    (isError) as data rather than an exception; transport failures still raise.
    Module-level so tests can monkeypatch the bridge hop."""
    async with asyncio.timeout(timeout + 5):  # backstop over the per-call timeout
        async with Client(url, timeout=timeout) as client:
            return await client.call_tool(
                name, arguments, timeout=timeout, raise_on_error=False
            )


@router.post("/servers/{server_id}/tools/{tool_name}/call", response_model=ToolCallResult)
async def call_server_tool(
    server_id: str,
    tool_name: str,
    request: Request,
    payload: ToolCallRequest = Body(default_factory=ToolCallRequest),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """Invoke one tool on a RUNNING server's bridge (the UI playground).

    Runs over the control plane (admin-token gated like every /api server route), so
    trying a tool never needs a data-plane bearer token. MCP error semantics are
    mirrored: a tool's own failure comes back as ``is_error`` in a 200, while an
    unreachable/broken bridge is a 502 and a timeout a 504."""
    _visible(principal, session, server_id)
    sup = request.app.state.supervisor
    unit = sup.unit(server_id)
    if unit is None or unit.state != "running" or unit.port is None:
        raise HTTPException(status_code=409, detail="server is not running")
    if not any(t.get("name") == tool_name for t in unit.tools or []):
        raise HTTPException(status_code=404, detail=f"tool {tool_name!r} not found")
    timeout = min(max(float(payload.timeout_s), 1.0), 300.0)
    started = time.monotonic()
    # In-flight bookkeeping for the WHOLE awaited call (like the /s proxy path):
    # a long-running tool execution must not have its bridge quiesced from under
    # it by the idle sweep. request_finished also restarts the idle clock.
    sup.request_started(server_id)
    try:
        result = await _call_bridge_tool(
            f"http://{unit.host}:{unit.port}/mcp", tool_name, payload.arguments, timeout
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise HTTPException(
            status_code=504, detail=f"tool call timed out after {timeout:g}s"
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"tool call failed: {exc}") from exc
    finally:
        sup.request_finished(server_id)
    structured = result.structured_content
    return ToolCallResult(
        is_error=bool(result.is_error),
        content=[block.model_dump(mode="json") for block in result.content or []],
        structured_content=structured if isinstance(structured, dict) else None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.get("/servers/{server_id}/logs")
async def stream_logs(
    server_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """SSE stream of a server's live bridge logs.

    Replays the current activation's bounded in-memory backlog, then tails new
    lines until the client disconnects or that activation is replaced. Terminal
    activation logs remain available until Retry, edit, disable, or delete.
    404 if the server row doesn't exist (or isn't visible to the caller).
    """
    _visible(principal, session, server_id)

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
