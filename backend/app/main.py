"""FastAPI app factory — control plane + reverse proxy + static SPA in one process.

Route order matters: the API (/api/*) and per-server proxy (/s/*) are registered
BEFORE the SPA catch-all mount, so they win over the client-side router. The
supervisor + reconciler run as a background task tied to the app lifespan.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api import auth as auth_api
from app.api import health as health_api
from app.api import servers as servers_api
from app.api import settings as settings_api
from app.api import tokens as tokens_api
from app.auth.control_plane import (
    ensure_control_token,
    enforcement_enabled,
    mint_control_token,
    require_control_plane,
)
from app.auth.middleware import (
    host_allowed,
    is_loopback_client,
    private_lan_allowed,
    request_allowlist,
)
from app.config import get_settings
from app.db import get_engine, init_db, repo
from app.proxy.router import router as proxy_router
from app.registry import service
from app.registry import settings as runtime_settings
from app.supervisor.supervisor import Supervisor


_UNSET = object()


def _bootstrap_private_lan() -> None:
    """Seed the ``allow_private_lan`` runtime setting from ``MCPE_ALLOW_PRIVATE_LAN``
    on first boot, so a headless box (no loopback browser to reach the UI) can enable
    LAN access declaratively. Seeds only when the setting has never been written, so a
    later UI toggle stays authoritative across restarts. Runs before the control-plane
    bootstrap so the minted admin token reflects the now-on enforcement."""
    if not get_settings().allow_private_lan:
        return
    with Session(get_engine()) as session:
        if repo.setting_get(session, "allow_private_lan", _UNSET) is _UNSET:
            runtime_settings.write(session, {"allow_private_lan": True})
            # The control-plane bootstrap below prints a 127.0.0.1 login URL (base_url
            # rewrites the 0.0.0.0 bind to loopback). That's wrong for a LAN device
            # reading these logs, so point at the box's own LAN address instead.
            port = get_settings().port
            print(
                f"[mcpelevator] MCPE_ALLOW_PRIVATE_LAN set — LAN access enabled. "
                f"Log in from a LAN device at  http://<this-box-LAN-IP>:{port}/login  "
                f"(see the admin-token notice below).",
                flush=True,
            )


def _bootstrap_control_plane_auth() -> None:
    """On boot, if control-plane auth is enforced, make sure the operator has a usable
    admin credential and is told how to get it — so a headless/compose deployment is
    not locked out. Mints one control token when none exists (idempotent across
    restarts); when one already exists we can't reprint its plaintext, so point at
    recovery instead of minting a second on every boot; when MCPE_ADMIN_TOKEN is set
    it is the credential and nothing is minted."""
    with Session(get_engine()) as session:
        if not enforcement_enabled(session):
            return
        if get_settings().admin_token:
            print("[mcpelevator] control-plane auth is ON, using MCPE_ADMIN_TOKEN", flush=True)
            return
        forced = get_settings().mint_admin_token
        token = mint_control_token(session) if forced else ensure_control_token(session)
    if token:
        credential = f"\n  Admin token (shown once, store it now):  {token}"
        if forced:
            credential += (
                "\n  (minted via MCPE_MINT_ADMIN_TOKEN — unset it after saving, or a new"
                "\n  token is minted on every restart.)"
            )
    else:
        # A control token already exists (a prior boot minted it, or it was created in
        # the UI) but we don't hold its plaintext to reprint. Don't silently swallow
        # the notice — the LAN seed above promised one — and don't rotate, which would
        # invalidate tokens already in use; point the operator at recovery instead.
        credential = (
            "\n  An admin token already exists — log in with the one you saved, set"
            "\n  MCPE_ADMIN_TOKEN to a known value, or rotate it from Settings → Security."
        )
    bar = "=" * 72
    print(
        f"\n{bar}\n  mcpelevator control-plane auth is ON."
        f"{credential}"
        f"\n  Log in at:  {get_settings().base_url}/login\n{bar}\n",
        flush=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(get_engine()) as session:
        service.normalize_auth_providers(session)  # canonicalize legacy auth_provider values
        service.backfill_config_hashes(session)  # rehash upgraded rows -> no spurious restarts
    _bootstrap_private_lan()  # seed LAN access from env before deciding auth enforcement
    _bootstrap_control_plane_auth()
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
        # Guard the control plane (/api) with the same Host/Origin allowlist as the
        # proxy (DNS-rebinding defense), in every mode: a loopback Host passes only
        # when the peer connects from loopback, and expose adds the configured hosts.
        # bind_mode controls only the network bind, so a DNS-rebound page could
        # otherwise hit loopback and mint tokens / change settings. This is the
        # first of two layers; require_control_plane (on the sensitive routers)
        # adds per-request bearer auth when enforcement is on.
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            with Session(get_engine()) as session:
                allowed = request_allowlist(session)
                allow_private = private_lan_allowed(request, session)
            ok, reason = host_allowed(
                request.headers.get("host", ""),
                request.headers.get("origin"),
                allowed,
                client_is_loopback=is_loopback_client(request),
                allow_private=allow_private,
            )
            if not ok:
                return JSONResponse({"detail": reason}, status_code=403)
        return await call_next(request)

    # health and auth-status stay public; the sensitive routers require a control
    # token when enforcement is on (require_control_plane is a no-op otherwise).
    app.include_router(health_api.router, prefix="/api")
    app.include_router(auth_api.router, prefix="/api")
    gated = [Depends(require_control_plane)]
    app.include_router(servers_api.router, prefix="/api", dependencies=gated)
    app.include_router(tokens_api.router, prefix="/api", dependencies=gated)
    app.include_router(settings_api.router, prefix="/api", dependencies=gated)
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
