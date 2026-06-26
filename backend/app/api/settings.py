"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.api.schemas import SettingsInfo, SettingsUpdate
from app.auth.control_plane import would_lock_out
from app.db import get_session
from app.registry import settings as runtime_settings

router = APIRouter()


@router.get("/settings", response_model=SettingsInfo)
async def get_settings(session: Session = Depends(get_session)):
    return SettingsInfo(**runtime_settings.read_all(session))


@router.patch("/settings", response_model=SettingsInfo)
async def update_settings(payload: SettingsUpdate, session: Session = Depends(get_session)):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    # Refuse a change that switches control-plane auth on while no admin token exists:
    # the next request (including POST /tokens) would be gated and lock the operator
    # out. The UI guards this too; this is the server-side backstop.
    if would_lock_out(session, changes):
        raise HTTPException(
            status_code=400,
            detail="generate an admin token before enabling control-plane auth",
        )
    try:
        # All invariants (enum settings + the host allowlist) are enforced in the
        # SSOT writer; surface its ValueError as a 400.
        return SettingsInfo(**runtime_settings.write(session, changes))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
