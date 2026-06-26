"""The single auth enforcement point.

The reverse proxy calls ``enforce(request, server)`` before forwarding. Provider
selection is data-driven (per-server ``auth_provider``, falling back to a default),
so new providers register here without touching routing. Host/Origin allowlist
(DNS-rebinding defense) and the ``bearer`` provider arrive in M5.
"""

from __future__ import annotations

from starlette.requests import Request

from app.auth.none import NoneProvider
from app.db.models import Server

# registry of providers by name (bearer/oauth added in later milestones)
_PROVIDERS = {
    "none": NoneProvider(),
}
_DEFAULT = "none"


def resolve(server: Server):
    name = server.auth_provider
    if name == "inherit":
        name = _DEFAULT
    return _PROVIDERS.get(name, _PROVIDERS["none"])


async def enforce(request: Request, server: Server) -> None:
    await resolve(server).authenticate(request, server)
