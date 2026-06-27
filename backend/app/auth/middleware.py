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


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP literal, tolerating a ``[...]`` IPv6 wrapper and a ``%zone`` scope
    id — neither of which ``ipaddress.ip_address`` accepts, yet both can appear (a
    bracketed Host, or a link-local peer like ``fe80::1%eth0``). Returns None for a
    hostname or anything unparseable, so callers fail closed."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    value = value.split("%", 1)[0]  # drop the zone index ipaddress can't read
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


# Private (non-globally-routable) LAN ranges the allowance trusts. Explicit ranges,
# NOT ``ipaddress.is_private`` — that also matches special-use ranges like TEST-NET
# (203.0.113.0/24) and IPv6 documentation (2001:db8::/32), which aren't real LANs.
# Mirrors the frontend's ``isPrivateIpHost`` and the ranges named in the README so the
# UI lock-out guard and the backend agree on what counts as "local". Loopback is NOT
# here — it's honoured only via the peer-gated ``_LOOPBACK`` path / a loopback peer.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # IPv4 link-local
        "fc00::/7",  # IPv6 unique-local (ULA)
        "fe80::/10",  # IPv6 link-local
    )
)


def _is_private_lan_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _PRIVATE_NETWORKS)


def is_private_client(request: Request) -> bool:
    """True when the request's *real* peer is on a private/loopback network (RFC 1918,
    link-local, IPv6 ULA, loopback).

    Used by the ``allow_private_lan`` allowance: a self-hosted box on a home LAN (e.g.
    Unraid) should be reachable from other devices on the same network. The peer check
    is what keeps that safe — only a request that actually originates on a private
    network may use a private-IP ``Host`` to pass the guard.

    A configured ``MCPE_TRUSTED_PROXIES`` forwarder is EXCLUDED, even when its own
    address is private (e.g. the Docker bridge gateway ``172.20.0.1``): behind NAT/SNAT
    the observed peer is the forwarder, which can't vouch for the real client, so trusting
    it would let SNAT'd public traffic satisfy this gate. The trusted-proxy convenience
    stays scoped to the loopback allowance; LAN access needs the real client IP visible
    (host networking) — see docker-compose. A genuine loopback peer always qualifies.
    """
    client = request.client
    if client is None:
        return False
    ip = _parse_ip(client.host)
    if ip is None:
        # Non-IP peer: Starlette's TestClient reports "testclient", and a bare
        # "localhost" can appear in some setups — neither is forgeable as a TCP source
        # by a remote client, so treat them as loopback.
        return client.host in {"localhost", "testclient"}
    if ip.is_loopback:
        return True
    if not _is_private_lan_ip(ip):
        return False
    # Private range, but reject it if it's a known forwarder address (see above).
    return not any(ip in net for net in _trusted_networks(get_settings().trusted_proxies))


def _is_private_host_literal(host: str) -> bool:
    """True when ``host`` is a private-LAN IP *literal* (not a hostname).

    Restricting the ``allow_private_lan`` allowance to IP literals is what makes it
    DNS-rebinding-safe: a rebinding attack delivers the attacker's *domain* in the
    Host header (``Host: evil.example``), which is never a bare private-IP literal,
    so it can't satisfy this check even after the name rebinds to a LAN address.

    Loopback (127.0.0.1, ::1) and the unspecified address (0.0.0.0, ::) are excluded —
    they're not in ``_PRIVATE_NETWORKS`` — so a private peer can't reach the box by
    spoofing ``Host: 127.0.0.1`` without actually connecting from loopback.
    """
    ip = _parse_ip(host)
    return ip is not None and _is_private_lan_ip(ip)


def private_lan_allowed(request: Request, session: Session) -> bool:
    """Whether this request may use the private-LAN allowance: the runtime setting
    is on AND the peer is on a private/loopback network. Computed once and threaded
    into ``host_allowed`` so the proxy and control-plane guards agree."""
    return runtime_settings.allow_private_lan(session) and is_private_client(request)


def host_allowed(
    host_header: str,
    origin_header: str | None,
    allowed: list[str],
    *,
    client_is_loopback: bool,
    allow_private: bool = False,
) -> tuple[bool, str]:
    """Pure allowlist check (unit-tested). Returns (ok, reason-if-not).

    Loopback hostnames are honoured only when ``client_is_loopback`` is true;
    otherwise an off-host client could send ``Host: localhost`` and pass without
    ever being in the allowlist. The flag is keyword-only and required so a caller
    cannot silently drop it and re-open that bypass.

    When ``allow_private`` is true (the ``allow_private_lan`` setting, gated on a
    private-network peer by the caller), a private-IP *literal* Host/Origin also
    passes — the rebinding-safe path for reaching the box from another device on the
    same LAN. It stays keyword-only with a safe default for the same reason.
    """
    allowset = {h for h in (host_only(x) for x in allowed) if h}
    if client_is_loopback:
        allowset |= _LOOPBACK

    def ok(name: str) -> bool:
        return name in allowset or (allow_private and _is_private_host_literal(name))

    host = host_only(host_header)
    if not host:
        return False, "missing host header"  # fail closed: no Host must not pass
    if not ok(host):
        return False, f"host {host!r} not in allowlist"
    if origin_header:
        origin = host_only(origin_header)
        if not origin:
            return False, "invalid origin header"  # present but unparseable -> fail closed
        if not ok(origin):
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
        allow_private = private_lan_allowed(request, session)

    # Always validate Host/Origin (DNS-rebinding defense). In local mode only loopback
    # and the configured public host pass; expose adds the runtime allowlist too, and
    # allow_private_lan adds private-IP-literal hosts from a private-network peer.
    ok, reason = host_allowed(
        request.headers.get("host", ""),
        request.headers.get("origin"),
        allowed,
        client_is_loopback=is_loopback_client(request),
        allow_private=allow_private,
    )
    if not ok:
        raise HTTPException(status_code=403, detail=reason)

    await resolve(server, default).authenticate(request, server)
