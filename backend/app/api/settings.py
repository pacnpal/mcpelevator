"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.api.schemas import SettingsInfo, SettingsUpdate
from app.db import get_session
from app.registry import settings as runtime_settings

router = APIRouter()


@router.get("/settings", response_model=SettingsInfo)
async def get_settings(session: Session = Depends(get_session)):
    return SettingsInfo(**runtime_settings.read_all(session))


@router.patch("/settings", response_model=SettingsInfo)
async def update_settings(payload: SettingsUpdate, session: Session = Depends(get_session)):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        # All invariants (enum settings + the host allowlist) are enforced in the
        # SSOT writer; surface its ValueError as a 400.
        return SettingsInfo(**runtime_settings.write(session, changes))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
