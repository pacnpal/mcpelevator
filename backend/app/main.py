"""FastAPI app factory — control plane + reverse proxy + static SPA in one process.

Route order matters: the API (/api/*) and per-server proxy (/s/*) are registered
BEFORE the SPA catch-all mount, so they win over the client-side router. The
supervisor + reconciler run as a background task tied to the app lifespan.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api import auth as auth_api
from app.api import catalog as catalog_api
from app.api import groups as groups_api
from app.api import health as health_api
from app.api import servers as servers_api
from app.api import settings as settings_api
from app.api import tokens as tokens_api
from app.api import users as users_api
from app.auth.control_plane import (
    ensure_control_token,
    enforcement_enabled,
    mint_control_token,
    require_control_plane,
)
from app.auth.middleware import enforce_host
from app.config import get_settings
from app.db import get_engine, init_db, repo
from app.groups import registry as group_registry
from app.groups.hub import GroupHub
from app.groups.route import GroupDispatch
from app.proxy.router import router as proxy_router
from app.registry import service
from app.registry import settings as runtime_settings
from app.supervisor.supervisor import Supervisor


_UNSET = object()


def _seed_setting_from_env(enabled: bool, key: str, message: str) -> None:
    """Seed a runtime setting from its ``MCPE_*`` env flag on first boot, so a headless
    box (no loopback browser to reach the UI) can enable it declaratively. Seeds only
    when the setting has never been written, so a later UI toggle stays authoritative
    across restarts."""
    if not enabled:
        return
    with Session(get_engine()) as session:
        if repo.setting_get(session, key, _UNSET) is _UNSET:
            runtime_settings.write(session, {key: True})
            print(message, flush=True)


def _bootstrap_private_lan() -> None:
    # Runs before the control-plane bootstrap so the minted admin token reflects the
    # now-on enforcement. That bootstrap prints a 127.0.0.1 login URL (base_url rewrites
    # the 0.0.0.0 bind to loopback) — wrong for a LAN device reading these logs, so
    # point at the box's own LAN address instead.
    _seed_setting_from_env(
        get_settings().allow_private_lan,
        "allow_private_lan",
        f"[mcpelevator] MCPE_ALLOW_PRIVATE_LAN set — LAN access enabled. "
        f"Log in from a LAN device at  http://<this-box-LAN-IP>:{get_settings().port}/login  "
        f"(see the admin-token notice below).",
    )


def _bootstrap_docker_runner() -> None:
    _seed_setting_from_env(
        get_settings().docker_runner,
        "docker_runner",
        "[mcpelevator] MCPE_DOCKER_RUNNER set — docker runner enabled "
        "(root-equivalent; runs images on the mounted Docker daemon).",
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
        service.normalize_docker_servers(session)  # canonicalize legacy docker rows before enable
        service.normalize_reserved_slugs(session)  # rename rows a reserved slug would shadow
        service.backfill_config_hashes(session)  # rehash upgraded rows -> no spurious restarts
        # The group registry is the single source of truth for /g/<name>. Validate it
        # against the server table before serving: an unknown member id means an
        # inconsistent (hand-edited) config, and we refuse to boot rather than serve it.
        group_registry.validate_at_startup(session)
    _bootstrap_private_lan()  # seed LAN access from env before deciding auth enforcement
    _bootstrap_docker_runner()  # seed docker-runner enable from env (headless)
    _bootstrap_control_plane_auth()
    app.state.http = httpx.AsyncClient(timeout=None)  # no timeout: long-lived SSE streams
    supervisor = Supervisor()
    await supervisor.boot_reset()  # observed runtime from a prior process is stale
    app.state.supervisor = supervisor
    # The group hub (constructed in create_app) tracks the running topology: every
    # reconcile pass converges each group's mounted set.
    supervisor.on_converged = lambda: app.state.groups.sync(supervisor)
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
        await app.state.groups.close()  # stop each group's session manager
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
    """
    Create the FastAPI application.
    
    Includes the public and control-plane API routers, the reverse-proxy routes, and either the built SPA or a JSON root response when the frontend is unavailable.
    
    Returns:
    	app (FastAPI): The configured application instance.
    """
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
                try:
                    enforce_host(request, session)
                except HTTPException as exc:
                    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    # health and auth-status stay public; the sensitive routers require a control
    # token when enforcement is on (require_control_plane is a no-op otherwise).
    app.include_router(health_api.router, prefix="/api")
    app.include_router(auth_api.router, prefix="/api")
    # RFC 9728 Protected Resource Metadata for oauth-protected servers — public by
    # design (clients fetch it pre-auth), no /api prefix, registered before the SPA.
    from app.auth.oauth import wellknown as oauth_wellknown

    app.include_router(oauth_wellknown)
    gated = [Depends(require_control_plane)]
    app.include_router(servers_api.router, prefix="/api", dependencies=gated)
    app.include_router(catalog_api.router, prefix="/api", dependencies=gated)
    app.include_router(tokens_api.router, prefix="/api", dependencies=gated)
    app.include_router(settings_api.router, prefix="/api", dependencies=gated)
    # Groups and user management are global, admin-owned surfaces (their routers
    # also declare require_admin themselves — defense in depth at include time).
    app.include_router(groups_api.router, prefix="/api", dependencies=gated)
    app.include_router(users_api.router, prefix="/api", dependencies=gated)
    # Group endpoints (/g/<name>/mcp) BEFORE the proxy catch-all and the SPA mount:
    # registration order wins. The hub is constructed here (state for the dispatcher)
    # but only starts serving groups once the lifespan wires it to the supervisor.
    hub = GroupHub()
    app.state.groups = hub
    app.mount("/g", GroupDispatch(hub))
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
