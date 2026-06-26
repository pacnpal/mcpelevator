"""SSE-safe reverse proxy: /s/<slug>/{mcp,rest/*} -> the server's loopback bridge.

This is also the single auth + (later) Host/Origin enforcement chokepoint. The
response is streamed, never buffered, so Streamable HTTP / SSE works: we forward
the request body (small JSON-RPC), then relay the upstream response chunk-by-chunk
with ``X-Accel-Buffering: no`` and content-encoding stripped.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from sqlmodel import Session
from starlette.responses import Response, StreamingResponse

from app.auth.middleware import enforce
from app.db import get_engine, repo

router = APIRouter()

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

    endpoint = request.app.state.supervisor.endpoint(slug)
    if endpoint is None:
        return Response("server not running", status_code=503)
    host, port = endpoint

    query = request.url.query
    target = f"http://{host}:{port}/{path}" + (f"?{query}" if query else "")
    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}

    client: httpx.AsyncClient = request.app.state.http
    upstream = await client.send(
        client.build_request(request.method, target, headers=fwd_headers, content=body),
        stream=True,
    )

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

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
