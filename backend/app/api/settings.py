"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.api.schemas import SettingsInfo, SettingsUpdate
from app.auth.control_plane import would_lock_out
from app.db import get_session
from app.registry import settings as runtime_settings

router = APIRouter()


@router.get("/settings", response_model=SettingsInfo)
async def get_settings(session: Session = Depends(get_session)):
    return SettingsInfo(**runtime_settings.read_all(session))


@router.patch("/settings", response_model=SettingsInfo)
async def update_settings(
    payload: SettingsUpdate, request: Request, session: Session = Depends(get_session)
):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}

    def guard(s: Session) -> None:
        # Re-checked inside the settings write transaction (under the write lock), so a
        # token delete racing this enable can't remove the last control credential
        # between the check and the commit. Refuse to switch enforcement on unless THIS
        # request still authenticates as control; the UI guards this too.
        if would_lock_out(request, s, changes):
            raise HTTPException(
                status_code=400,
                detail="authenticate with an admin token before enabling control-plane auth",
            )

    try:
        # Invariants (enum settings + the host allowlist) are enforced in the SSOT
        # writer; the guard runs under its write lock. Surface ValueError as a 400.
        result = SettingsInfo(**runtime_settings.write(session, changes, guard=guard))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Toggling the root-equivalent docker runner is a gate change — nudge the reconciler so it
    # applies at once (stop running docker units when turned off) instead of waiting for the
    # next poll interval, which an operator may have lengthened.
    if "docker_runner" in changes:
        request.app.state.supervisor.nudge()
    return result
