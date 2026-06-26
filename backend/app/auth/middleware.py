"""The single auth enforcement point.

The reverse proxy calls ``enforce(request, server)`` before forwarding. Two checks
happen here, in one place, for every exposed request:

1. **Host/Origin allowlist** (DNS-rebinding defense), enforced in every mode.
   Loopback is allowed only when the client actually connects from loopback, and
   ``expose`` mode adds the configured allowlist. A request whose Host/Origin is
   neither is rejected. ``bind_mode`` controls only the network bind, not
   reachability (a DNS-rebound page can still target loopback), so the Host check
   is not gated on it.
2. **Auth provider** — chosen per-server (``auth_provider``), falling back to the
   ``default_auth_provider`` setting when the server is ``inherit``. New providers
   register in ``_PROVIDERS`` without touching routing.
"""

from __future__ import annotations

import functools
import ipaddress

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.auth.bearer import BearerProvider
from app.auth.none import NoneProvider
from app.config import get_settings
from app.db import get_engine
from app.db.models import Server
from app.registry import settings as runtime_settings
from app.util import host_only

_PROVIDERS = {
    "none": NoneProvider(),
    "bearer": BearerProvider(),
}

# Loopback hostnames, trusted only when the peer itself connects from loopback
# (see ``is_loopback_client``); otherwise an off-host client could spoof them.
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


@functools.lru_cache(maxsize=4)
def _trusted_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse MCPE_TRUSTED_PROXIES (comma-separated CIDRs), skipping malformed entries.
    Cached by the raw string (the env is fixed for the process lifetime)."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            pass
    return tuple(nets)


def is_loopback_client(request: Request) -> bool:
    """True when the request's peer is loopback (or a configured trusted proxy).

    A ``Host: localhost`` / ``127.0.0.1`` header must not be trusted from an off-host
    client (e.g. a Docker ``0.0.0.0`` bind reachable from the LAN), so the implicit
    loopback allowance in ``host_allowed`` is granted only to real loopback peers — or
    to a peer inside ``MCPE_TRUSTED_PROXIES`` (e.g. the Docker bridge gateway that
    forwards a loopback-published port, where the real source is already loopback).
    """
    client = request.client
    if client is None:
        return False
    try:
        ip = ipaddress.ip_address(client.host)
    except ValueError:
        # Non-IP peer host. Starlette's TestClient reports "testclient", and a bare
        # "localhost" can appear in some setups; neither can be forged as a TCP
        # source address by a remote client, so treat them as loopback.
        return client.host in {"localhost", "testclient"}
    if ip.is_loopback:
        return True
    return any(ip in net for net in _trusted_networks(get_settings().trusted_proxies))


def host_allowed(
    host_header: str,
    origin_header: str | None,
    allowed: list[str],
    *,
    client_is_loopback: bool,
) -> tuple[bool, str]:
    """Pure allowlist check (unit-tested). Returns (ok, reason-if-not).

    Loopback hostnames are honoured only when ``client_is_loopback`` is true;
    otherwise an off-host client could send ``Host: localhost`` and pass without
    ever being in the allowlist. The flag is keyword-only and required so a caller
    cannot silently drop it and re-open that bypass.
    """
    allowset = {h for h in (host_only(x) for x in allowed) if h}
    if client_is_loopback:
        allowset |= _LOOPBACK
    host = host_only(host_header)
    if not host:
        return False, "missing host header"  # fail closed: no Host must not pass
    if host not in allowset:
        return False, f"host {host!r} not in allowlist"
    if origin_header:
        origin = host_only(origin_header)
        if not origin:
            return False, "invalid origin header"  # present but unparseable -> fail closed
        if origin not in allowset:
            return False, f"origin {origin!r} not in allowlist"
    return True, ""


def request_allowlist(session: Session) -> list[str]:
    """Hosts allowed beyond loopback for the Host/Origin guard: the runtime allowlist
    (only when ``bind_mode`` is ``expose``) plus the operator-configured public host.
    ``MCPE_PUBLIC_BASE_URL`` is an explicit "this is my URL" declaration, so its host
    is always trusted — otherwise the advertised public URL would 403 itself before
    the operator could add it to the allowlist."""
    mode = runtime_settings.bind_mode(session)
    allowed = list(runtime_settings.allowed_hosts(session)) if mode == "expose" else []
    public = get_settings().public_host
    if public:
        allowed.append(public)
    return allowed


async def enforce(request: Request, server: Server) -> None:
    with Session(get_engine()) as session:
        allowed = request_allowlist(session)
        default = runtime_settings.default_auth_provider(session)

    # Always validate Host/Origin (DNS-rebinding defense). In local mode only loopback
    # and the configured public host pass; expose adds the runtime allowlist too.
    ok, reason = host_allowed(
        request.headers.get("host", ""),
        request.headers.get("origin"),
        allowed,
        client_is_loopback=is_loopback_client(request),
    )
    if not ok:
        raise HTTPException(status_code=403, detail=reason)

    await resolve(server, default).authenticate(request, server)
