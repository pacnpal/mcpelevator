"""The ``none`` auth provider — allow all. Safe only with a localhost bind."""

from __future__ import annotations

from starlette.requests import Request

from app.db.models import Server


class NoneProvider:
    name = "none"

    async def authenticate(self, request: Request, server: Server) -> None:
        return None
