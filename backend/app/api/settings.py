"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.api.schemas import SettingsInfo, SettingsUpdate
from app.api.util import resync_groups
from app.auth.control_plane import would_lock_out
from app.db import get_session
from app.registry import settings as runtime_settings

router = APIRouter()


def _info(values: dict) -> SettingsInfo:
    # SettingsInfo declares the SPA-facing subset; the group registry is served by its
    # own /api/groups router, so drop any keys (e.g. "groups") not in the model.
    return SettingsInfo(**{k: values[k] for k in SettingsInfo.model_fields if k in values})


@router.get("/settings", response_model=SettingsInfo)
async def get_settings(session: Session = Depends(get_session)):
    return _info(runtime_settings.read_all(session))


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

    auth_changed = "default_auth_provider" in changes
    try:
        if auth_changed:
            # Hold the hub lock across the write so the reconciler cannot rebuild an
            # old bundle between invalidation and commit. Requests see 503 until the
            # new provider's member set is ready.
            async with request.app.state.groups.auth_transition():
                result = _info(runtime_settings.write(session, changes, guard=guard))
        else:
            # Invariants (enum settings + the host allowlist) are enforced in the SSOT
            # writer; the guard runs under its write lock. Surface ValueError as a 400.
            result = _info(runtime_settings.write(session, changes, guard=guard))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if auth_changed:
            await resync_groups(request)
    # Gate changes apply at once instead of waiting for the next poll interval (which an
    # operator may have lengthened): docker_runner stops running docker units via the
    # nudged reconcile.
    if "docker_runner" in changes:
        request.app.state.supervisor.nudge()
    # A default-auth change must converge the group hub BEFORE this returns, not on the
    # next reconcile: every /g dispatcher enforces the NEW default auth on the very next
    # request, so a bearer->none downgrade must not leave a group's OLD mounted set
    # (which may include bearer-only members) serveable in the gap. sync() is
    # lock-serialized and its lifespans run in their own tasks, so calling it here is safe.
    return result
