"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.aggregate.hub import AGGREGATE_SLUG
from app.api.schemas import SettingsInfo, SettingsUpdate
from app.api.util import base_url
from app.auth.control_plane import would_lock_out
from app.db import get_session
from app.registry import settings as runtime_settings

router = APIRouter()


def _info(request: Request, values: dict) -> SettingsInfo:
    info = SettingsInfo(**values)
    if info.unified_endpoint:
        # derived, read-only: the copyable aggregate URL (same base derivation as the
        # per-server copy menu)
        info.unified_endpoint_url = f"{base_url(request)}/s/{AGGREGATE_SLUG}/mcp"
    return info


@router.get("/settings", response_model=SettingsInfo)
async def get_settings(request: Request, session: Session = Depends(get_session)):
    return _info(request, runtime_settings.read_all(session))


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
        result = _info(request, runtime_settings.write(session, changes, guard=guard))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Gate changes apply at once instead of waiting for the next poll interval (which an
    # operator may have lengthened): docker_runner stops running docker units, and the
    # unified-endpoint settings (incl. default_auth_provider, which drives its inclusion
    # rule) converge the aggregate via the post-reconcile hook.
    if changes.keys() & {
        "docker_runner", "unified_endpoint", "unified_servers", "default_auth_provider"
    }:
        request.app.state.supervisor.nudge()
    return result
