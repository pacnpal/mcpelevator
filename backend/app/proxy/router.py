"""SSE-safe reverse proxy: /s/<slug>/{mcp,rest/*} -> the server's loopback bridge.

This is also the single auth + (later) Host/Origin enforcement chokepoint. The
response is streamed, never buffered, so Streamable HTTP / SSE works: we forward
the request body (small JSON-RPC), then relay the upstream response chunk-by-chunk
with ``X-Accel-Buffering: no`` and content-encoding stripped.

Every proxied request also feeds the supervisor's idle bookkeeping: traffic marks
the server active, and a request for a quiesced ("idle") server WAKES it — the
proxy holds the request until the bridge is ready (bounded by the same startup
timeout the activation itself gets) instead of bouncing the client with a 503.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Request
from sqlmodel import Session
from starlette.responses import Response, StreamingResponse

from app.auth.middleware import enforce
from app.config import get_settings
from app.db import get_engine, repo

router = APIRouter()

# How often the wake path re-checks for the woken bridge's endpoint.
_WAKE_POLL_S = 0.25


async def _await_wake(sup, server_id: str, slug: str, timeout: float):
    """Wait for a just-woken server's endpoint, or ``None`` on timeout/failure.

    Bails out early when the activation lands terminally (failed) — waiting out the
    full window for a server that already gave up would just hang the client."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        endpoint = sup.endpoint(slug)
        if endpoint is not None:
            return endpoint
        unit = sup.unit(server_id)
        if unit is not None and unit.state == "failed" and unit.startup_status is None:
            return None
        await asyncio.sleep(_WAKE_POLL_S)
    return None

# hop-by-hop headers (RFC 7230) + ones the proxy must own
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

_PROXY_METHODS = ["GET", "POST", "DELETE", "OPTIONS", "PUT", "PATCH"]


@router.api_route("/s/{slug}/{path:path}", methods=_PROXY_METHODS)
async def proxy(slug: str, path: str, request: Request) -> Response:
    with Session(get_engine()) as session:
        server = repo.get_server_by_slug(session, slug)
    if server is None:
        return Response("unknown server", status_code=404)

    # auth chokepoint (raises 401/403 if denied)
    await enforce(request, server)

    sup = request.app.state.supervisor
    # Only authenticated traffic counts as activity — an unauthorized probe must not
    # keep a server awake (enforce() above raises before we get here).
    sup.mark_activity(server.id)

    endpoint = sup.endpoint(slug)
    if endpoint is None and (
        # Wake-on-request: the server was quiesced for inactivity. Hold the request
        # while the activation brings the bridge back (npx/uvx warm starts are
        # seconds; the bound matches one startup attempt's readiness window).
        sup.wake(server.id)
        # ...or an activation is ALREADY converging (a concurrent request's wake, an
        # operator start): the first wake's request clears the idle marker before
        # readiness, so a second request in that window must latch onto the same
        # activation instead of 503ing mid-cold-start.
        or (
            server.enabled
            and (
                sup.activation_requested_at(server.id) is not None
                or (
                    (unit := sup.unit(server.id)) is not None
                    and unit.state == "starting"
                )
            )
        )
    ):
        endpoint = await _await_wake(
            sup, server.id, slug, timeout=float(get_settings().start_timeout_s)
        )
    if endpoint is None:
        return Response(
            "server not running", status_code=503, headers={"retry-after": "5"}
        )
    host, port = endpoint

    # Count the request as in flight from the moment a bridge is selected until
    # the response stream closes — covering the body read too, not just the
    # dispatch: a slow upload or a long-held Streamable-HTTP/SSE stream can each
    # outlast the idle window, and the sweep must not stop the bridge underneath
    # either. request_finished (in relay's finally, or the failure path here)
    # also restarts the idle clock, so the window is measured from stream close.
    sup.request_started(server.id)
    try:
        query = request.url.query
        target = f"http://{host}:{port}/{path}" + (f"?{query}" if query else "")
        body = await request.body()
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}

        client: httpx.AsyncClient = request.app.state.http
        upstream = await client.send(
            client.build_request(request.method, target, headers=fwd_headers, content=body),
            stream=True,
        )
    except BaseException:
        sup.request_finished(server.id)
        raise

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP and k.lower() != "content-encoding"
    }
    resp_headers["x-accel-buffering"] = "no"  # tell any outer proxy not to buffer SSE

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            sup.request_finished(server.id)

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
