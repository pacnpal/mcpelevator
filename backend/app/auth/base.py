"""Auth seam.

A single, swappable interface enforced at one chokepoint (the reverse proxy).
Adding a provider (bearer, OAuth, mTLS, …) is a new small module — routing never
changes. v1 ships ``none`` + ``bearer`` (M5); the protocol is the contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from starlette.requests import Request

from app.db.models import Server


@runtime_checkable
class AuthProvider(Protocol):
    name: str

    async def authenticate(self, request: Request, server: Server) -> None:
        """Allow the request, or raise fastapi.HTTPException(401/403)."""
        ...
