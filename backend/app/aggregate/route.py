"""ASGI dispatcher for the unified endpoint, mounted at ``/s/all``.

Registered BEFORE the ``/s/{slug}/{path:path}`` reverse-proxy route (registration
order wins), and the ``all`` slug is reserved so no real server can occupy it. The
dispatcher applies the exact same gate sequence as the per-server proxy — existence
(the setting), then ``enforce()`` (Host/Origin allowlist + per-server auth), then
liveness — before delegating to the hub's current FastMCP app. Starlette's Mount
strips the ``/s/all`` prefix, so the inner app (built with ``path="/mcp"``) sees
``/mcp``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from app.aggregate.hub import AGGREGATE_SERVER, AggregateHub
from app.auth.middleware import enforce
from app.db import get_engine
from app.registry import settings as runtime_settings


class AggregateEndpoint:
    def __init__(self, hub: AggregateHub) -> None:
        self._hub = hub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover — Mount only routes http here
            raise RuntimeError("AggregateEndpoint only handles http")
        request = Request(scope, receive)

        with Session(get_engine()) as session:
            enabled = runtime_settings.unified_endpoint(session)
        if not enabled:
            # indistinguishable from a nonexistent slug (same body as the proxy's 404)
            await Response("unknown server", status_code=404)(scope, receive, send)
            return

        try:
            await enforce(request, AGGREGATE_SERVER)
        except HTTPException as exc:
            # raw ASGI — render the exception the way FastAPI's handler would,
            # preserving WWW-Authenticate etc.
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
            await response(scope, receive, send)
            return

        inner = self._hub.app
        if inner is None:
            await Response("server not running", status_code=503)(scope, receive, send)
            return
        await inner(scope, receive, send)
