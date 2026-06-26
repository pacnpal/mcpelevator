"""FastAPI app factory — control plane + reverse proxy + static SPA in one process.

Route order matters: the API (/api/*) and per-server proxy (/s/*) are registered
BEFORE the SPA catch-all mount, so they win over the client-side router. The
supervisor + reconciler run as a background task tied to the app lifespan.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api import health as health_api
from app.api import servers as servers_api
from app.api import settings as settings_api
from app.api import tokens as tokens_api
from app.auth.middleware import host_allowed
from app.config import get_settings
from app.db import get_engine, init_db
from app.proxy.router import router as proxy_router
from app.registry import settings as runtime_settings
from app.supervisor.supervisor import Supervisor


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.http = httpx.AsyncClient(timeout=None)  # no timeout: long-lived SSE streams
    supervisor = Supervisor()
    supervisor.boot_reset()  # observed runtime from a prior process is stale
    app.state.supervisor = supervisor
    reconciler = asyncio.create_task(supervisor.run_forever())
    try:
        yield
    finally:
        await supervisor.shutdown()
        reconciler.cancel()
        try:
            await reconciler
        except asyncio.CancelledError:
            pass
        await app.state.http.aclose()


class SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback: unknown paths return index.html."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="mcpelevator", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def _control_plane_allowlist(request: Request, call_next):
        # In expose mode, guard the control plane (/api) with the same Host/Origin
        # allowlist as the proxy (DNS-rebinding defense). Per-request bearer auth on
        # /api is a deferred v1 item — it would also have to gate the SPA itself.
        if request.url.path.startswith("/api"):
            with Session(get_engine()) as session:
                if runtime_settings.bind_mode(session) == "expose":
                    ok, reason = host_allowed(
                        request.headers.get("host", ""),
                        request.headers.get("origin"),
                        runtime_settings.allowed_hosts(session),
                    )
                    if not ok:
                        return JSONResponse({"detail": reason}, status_code=403)
        return await call_next(request)

    app.include_router(health_api.router, prefix="/api")
    app.include_router(servers_api.router, prefix="/api")
    app.include_router(tokens_api.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(proxy_router)  # /s/{slug}/...

    fe = settings.frontend_dir
    if not fe.is_absolute():
        fe = (settings.backend_root / fe).resolve()
    if (fe / "index.html").exists():
        app.mount("/", SPAStaticFiles(directory=str(fe), html=True), name="spa")
    else:
        @app.get("/")
        async def root() -> JSONResponse:
            return JSONResponse(
                {"mcpelevator": __version__, "frontend": "not built", "api": "/api/health"}
            )

    return app


app = create_app()
