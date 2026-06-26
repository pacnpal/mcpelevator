"""Public control-plane auth status, so the SPA can decide whether to show login
instead of guessing from 401s. Reachable through the Host/Origin allowlist only;
it carries no secrets and reflects the current request's own credential."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session
from starlette.requests import Request

from app.api.schemas import AuthStatus
from app.auth.control_plane import control_auth, enforcement_enabled
from app.db import get_session

router = APIRouter()


@router.get("/auth/status", response_model=AuthStatus)
async def auth_status(request: Request, session: Session = Depends(get_session)) -> AuthStatus:
    return AuthStatus(
        enforced=enforcement_enabled(session),
        authenticated=control_auth(request, session) == "ok",
    )
