"""FastAPI app factory — control plane + reverse proxy + static SPA in one process.

Route order matters: the API (/api/*) and per-server proxy (/s/*) are registered
BEFORE the SPA catch-all mount, so they win over the client-side router. The
supervisor + reconciler run as a background task tied to the app lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from app import __version__, control_mcp, debug, logconfig, usage
from app.api import auth as auth_api
from app.api import catalog as catalog_api
from app.api import groups as groups_api
from app.api import health as health_api
from app.api import scim as scim_api
from app.api import servers as servers_api
from app.api import settings as settings_api
from app.api import tokens as tokens_api
from app.api import usage as usage_api
from app.api import users as users_api
from app.auth import cf_access
from app.catalog import mirror as catalog_mirror
from app.catalog import registry as catalog_registry
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


def _seed_setting_from_env(value: Any, key: str, message: str | None = None) -> None:
    """Seed a runtime setting from its ``MCPE_*`` env var on first boot, so a headless
    box (no loopback browser to reach the UI) can configure it declaratively. A falsy
    ``value`` means the env var is unset. Seeds only when the setting has never been
    written, so a later UI change stays authoritative across restarts. The value goes
    through the settings writer, so a bad one fails the boot with the reason."""
    if not value:
        return
    with Session(get_engine()) as session:
        if repo.setting_get(session, key, _UNSET) is _UNSET:
            runtime_settings.write(session, {key: value})
            if message:
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


def _bootstrap_oidc() -> None:
    """Seed the single sign-on settings from ``MCPE_OIDC_*``. The client secret isn't
    seeded: ``MCPE_OIDC_CLIENT_SECRET`` is read live and always wins. Enabling from the
    env skips the discovery check a Settings save runs; sign-in reports a bad provider."""
    env = get_settings()
    for key, value in (
        ("oidc_discovery_url", env.oidc_discovery_url),
        ("oidc_client_id", env.oidc_client_id),
        ("oidc_label", env.oidc_label),
        ("oidc_scopes", _split(env.oidc_scopes)),
        ("oidc_identity_claim", env.oidc_identity_claim),
        ("oidc_allowed_tenants", _split(env.oidc_allowed_tenants)),
        ("oidc_enabled", env.oidc_enabled),
    ):
        _seed_setting_from_env(value, key)
    # A falsy value means "unset" to _seed_setting_from_env, but false is this one's
    # point: seed it whenever the env var is set at all.
    if env.oidc_require_verified_email is not None:
        with Session(get_engine()) as session:
            if repo.setting_get(session, "oidc_require_verified_email", _UNSET) is _UNSET:
                runtime_settings.write(
                    session, {"oidc_require_verified_email": env.oidc_require_verified_email}
                )


def _split(raw: str) -> list[str]:
    """A comma-separated env value as a list, blanks dropped."""
    return [v.strip() for v in raw.split(",") if v.strip()]


def _bootstrap_oidc_groups() -> None:
    """Seed the SSO group mapping settings from ``MCPE_OIDC_*`` (lists comma-separated)."""
    env = get_settings()
    for key, value in (
        ("oidc_groups_claim", env.oidc_groups_claim),
        ("oidc_allowed_groups", _split(env.oidc_allowed_groups)),
        ("oidc_admin_groups", _split(env.oidc_admin_groups)),
        ("oidc_local_runner_groups", _split(env.oidc_local_runner_groups)),
        ("oidc_allowed_domains", _split(env.oidc_allowed_domains)),
    ):
        _seed_setting_from_env(value, key)


def _bootstrap_oidc_provisioning() -> None:
    """Seed just-in-time provisioning from ``MCPE_OIDC_PROVISIONING`` (true seeds it on)."""
    _seed_setting_from_env(get_settings().oidc_provisioning, "oidc_provisioning")


def _bootstrap_oauth_member_access() -> None:
    """Seed ``oauth_member_access`` from ``MCPE_OAUTH_MEMBER_ACCESS``."""
    _seed_setting_from_env(get_settings().oauth_member_access, "oauth_member_access")


def _bootstrap_oidc_session_max_age() -> None:
    """Seed the Sign-in session hard cap from ``MCPE_OIDC_SESSION_MAX_AGE_S``."""
    _seed_setting_from_env(get_settings().oidc_session_max_age_s, "oidc_session_max_age_s")


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
    if debug.enabled():
        # Here, not at import: uvicorn configures its loggers before the lifespan runs.
        debug.configure_logging(keep=True)
        # Never-awaited coroutines and callbacks that block the loop over 100ms.
        asyncio.get_running_loop().set_debug(True)
        debug.log_startup()
    else:
        logconfig.configure()
    logconfig.quiet_client_disconnects()
    init_db()
    cf_access.prewarm()
    with Session(get_engine()) as session:
        service.normalize_auth_providers(session)  # canonicalize legacy auth_provider values
        service.normalize_docker_servers(session)  # canonicalize legacy docker rows before enable
        service.normalize_reserved_slugs(session)  # rename rows a reserved slug would shadow
        service.backfill_config_hashes(session)  # rehash upgraded rows -> no spurious restarts
        if get_settings().control_mcp:
            created = service.ensure_builtin(session)  # every install gets one
            if created is not None:
                # Its slug is "mcpelevator" unless a server already took it; say where it is.
                print(
                    f"[mcpelevator] built-in control-plane MCP server ready at "
                    f"{get_settings().base_url}/s/{created.slug}/mcp",
                    flush=True,
                )
        # The group registry is the single source of truth for /g/<name>. Validate it
        # against the server table before serving: an unknown member id means an
        # inconsistent (hand-edited) config, and we refuse to boot rather than serve it.
        group_registry.validate_at_startup(session)
    _bootstrap_private_lan()  # seed LAN access from env before deciding auth enforcement
    _bootstrap_docker_runner()  # seed docker-runner enable from env (headless)
    _bootstrap_oidc()  # seed single sign-on settings from MCPE_OIDC_* (headless)
    _bootstrap_oidc_groups()
    _bootstrap_oidc_provisioning()
    _bootstrap_oidc_session_max_age()
    _bootstrap_oauth_member_access()
    _bootstrap_control_plane_auth()
    # No timeout: long-lived SSE streams. No connection cap either: the /s proxy holds a
    # pooled connection for every open client stream, so httpx's default cap of 100 was
    # reached by ordinary use, and then every proxied request hung waiting for a slot
    # and every catalog fetch died of PoolTimeout.
    app.state.http = httpx.AsyncClient(
        timeout=None, limits=httpx.Limits(max_connections=None, max_keepalive_connections=20)
    )
    supervisor = Supervisor()
    await supervisor.boot_reset()  # observed runtime from a prior process is stale
    app.state.supervisor = supervisor
    # The group hub (constructed in create_app) tracks the running topology: every
    # reconcile pass converges each group's mounted set.
    supervisor.on_converged = lambda: app.state.groups.sync(supervisor)
    reconciler = asyncio.create_task(supervisor.run_forever())
    # Usage counters accumulate in memory on the data-plane hot path; this task is
    # what ever writes them (and applies their retention).
    usage_flusher = asyncio.create_task(usage.run_forever())
    tasks = [reconciler, usage_flusher]
    if get_settings().catalog_mirror:
        sources = catalog_registry.enabled_sources()
        tasks.append(asyncio.create_task(catalog_mirror.run_forever(sources, app.state.http)))
    try:
        # Starlette doesn't run a route's ASGI lifespan: run /api/mcp's session manager here.
        control = app.state.control_mcp.inner
        async with control.router.lifespan_context(control):
            yield
    finally:
        await supervisor.shutdown()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await usage.flush()  # don't drop the last interval's counts on a clean stop
        except Exception:
            logging.getLogger(__name__).exception("final usage flush failed")
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
        path = request.url.path
        if path == "/api" or path.startswith(("/api/", scim_api.PREFIX + "/")):
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
    app.include_router(usage_api.router, prefix="/api", dependencies=gated)
    # Groups and user management are global, admin-owned surfaces (their routers
    # also declare require_admin themselves — defense in depth at include time).
    app.include_router(groups_api.router, prefix="/api", dependencies=gated)
    app.include_router(users_api.router, prefix="/api", dependencies=gated)
    # SCIM provisioning: its own scim-scoped token, never the control-plane gate.
    app.add_exception_handler(scim_api.ScimError, scim_api.error_response)
    app.include_router(scim_api.router)
    # Admin-only diagnostics bundle; 404 unless MCPE_DEBUG is on (app.debug).
    app.include_router(debug.router, prefix="/api")
    # The control plane as an MCP server; its tools call the routers above in-process
    # with the caller's credentials, so it gates and authorizes exactly like /api.
    app.state.control_mcp = control_mcp.build(app)
    app.add_route(control_mcp.PATH, app.state.control_mcp, methods=["GET", "POST", "DELETE"])
    # Group endpoints (/g/<name>/mcp) BEFORE the proxy catch-all and the SPA mount:
    # registration order wins. The hub is constructed here (state for the dispatcher)
    # but only starts serving groups once the lifespan wires it to the supervisor.
    hub = GroupHub()
    app.state.groups = hub
    app.mount("/g", GroupDispatch(hub))
    app.include_router(proxy_router)  # /s/{slug}/...

    if debug.enabled():
        app.add_middleware(debug.TraceMiddleware)  # outermost: sees every refusal too

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
