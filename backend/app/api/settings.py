"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.api.schemas import SettingsInfo, SettingsUpdate
from app.db import get_session
from app.registry import settings as runtime_settings

router = APIRouter()

_MODES = {"local", "expose"}
_PROVIDERS = {"none", "bearer"}


@router.get("/settings", response_model=SettingsInfo)
async def get_settings(session: Session = Depends(get_session)):
    return SettingsInfo(**runtime_settings.read_all(session))


@router.patch("/settings", response_model=SettingsInfo)
async def update_settings(payload: SettingsUpdate, session: Session = Depends(get_session)):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "bind_mode" in changes and changes["bind_mode"] not in _MODES:
        raise HTTPException(status_code=400, detail="bind_mode must be 'local' or 'expose'")
    if "default_auth_provider" in changes and changes["default_auth_provider"] not in _PROVIDERS:
        raise HTTPException(status_code=400, detail="default_auth_provider must be 'none' or 'bearer'")
    return SettingsInfo(**runtime_settings.write(session, changes))
