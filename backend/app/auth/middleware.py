"""The single auth enforcement point.

The reverse proxy calls ``enforce(request, server)`` before forwarding. Two checks
happen here, in one place, for every exposed request:

1. **Host/Origin allowlist** (DNS-rebinding defense) — only when ``bind_mode`` is
   ``expose``. Loopback is always allowed; otherwise the request's Host/Origin must
   be in the configured allowlist.
2. **Auth provider** — chosen per-server (``auth_provider``), falling back to the
   ``default_auth_provider`` setting when the server is ``inherit``. New providers
   register in ``_PROVIDERS`` without touching routing.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.auth.bearer import BearerProvider
from app.auth.none import NoneProvider
from app.db import get_engine
from app.db.models import Server
from app.registry import settings as runtime_settings

_PROVIDERS = {
    "none": NoneProvider(),
    "bearer": BearerProvider(),
}

# Loopback hostnames are always permitted (local-first default).
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def resolve(server: Server, default: str):
    name = server.auth_provider
    if name == "inherit":
        name = default
    provider = _PROVIDERS.get(name)
    if provider is None:
        # Fail closed: an unknown/corrupted provider must NOT silently disable auth.
        raise HTTPException(status_code=403, detail=f"unknown auth provider {name!r}")
    return provider


def _host_only(value: str) -> str:
    """Hostname from a Host header (``host[:port]`` / ``[ipv6][:port]``) or an
    Origin (``scheme://host[:port]``). urlsplit handles ports and IPv6 brackets."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        return (urlsplit(value).hostname or "").lower()
    return (urlsplit(f"//{value}").hostname or "").lower()


def host_allowed(host_header: str, origin_header: str | None, allowed: list[str]) -> tuple[bool, str]:
    """Pure allowlist check (unit-tested). Returns (ok, reason-if-not)."""
    allowset = _LOOPBACK | {h for h in (_host_only(x) for x in allowed) if h}
    host = _host_only(host_header)
    if not host:
        return False, "missing host header"  # fail closed: no Host must not pass
    if host not in allowset:
        return False, f"host {host!r} not in allowlist"
    if origin_header:
        origin = _host_only(origin_header)
        if origin and origin not in allowset:
            return False, f"origin {origin!r} not in allowlist"
    return True, ""


async def enforce(request: Request, server: Server) -> None:
    with Session(get_engine()) as session:
        mode = runtime_settings.bind_mode(session)
        allowed = runtime_settings.allowed_hosts(session) if mode == "expose" else []
        default = runtime_settings.default_auth_provider(session)

    if mode == "expose":
        ok, reason = host_allowed(request.headers.get("host", ""), request.headers.get("origin"), allowed)
        if not ok:
            raise HTTPException(status_code=403, detail=reason)

    await resolve(server, default).authenticate(request, server)
