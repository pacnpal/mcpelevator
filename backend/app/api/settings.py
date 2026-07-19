"""Runtime settings endpoints: bind mode, Host/Origin allowlist, default auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.api.schemas import SettingsInfo, SettingsUpdate
from app.api.util import resync_groups
from app.auth import principal as principal_mod
from app.auth.control_plane import (
    admin_credential_presented,
    enforcement_enabled,
    would_lock_out,
)
from app.auth.principal import Principal, require_admin
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


# GET stays readable by any authenticated principal — the SPA's add-server form
# reads docker_runner/default_auth_provider — but writes are admin-only.
@router.patch("/settings", response_model=SettingsInfo, dependencies=[Depends(require_admin)])
async def update_settings(
    payload: SettingsUpdate,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}

    def guard(s: Session) -> None:
        # Both checks run inside the settings write transaction (under the write
        # lock, AFTER the staged changes flushed), so nothing can change between
        # check and commit:
        # 1) Refuse to switch enforcement on unless THIS request presents an
        #    ADMIN-resolving credential (would_lock_out) — a token delete racing
        #    this enable can't remove the last admin credential between the check
        #    and the commit, and a member login can't flip enforcement on and
        #    strand the box; the UI guards this too.
        if would_lock_out(request, s, changes):
            raise HTTPException(
                status_code=400,
                detail="authenticate with an admin token before enabling control-plane auth",
            )
        # 2) Re-authorize the CALLER. Token-bound principals re-check their
        #    credential and role (a demotion/revocation commits under the config
        #    lock and must fail this queued write). The enforcement-off synthetic
        #    needs its own rule because enforcement_enabled here reads the STAGED
        #    values: when this very request is the one flipping enforcement on,
        #    the local operator is still the legitimate caller — provided they
        #    presented an admin credential (which check 1 already demanded). A
        #    CONCURRENT flip (committed by another request while this one queued)
        #    is caught the same way: enforcement now reads on, no admin credential
        #    on this request -> rejected.
        if principal is principal_mod.LOCAL_ADMIN:
            if enforcement_enabled(s) and not admin_credential_presented(request, s):
                raise HTTPException(status_code=403, detail="admin role required")
        elif not principal_mod.admin_now(s, principal):
            raise HTTPException(status_code=403, detail="admin role required")

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
    # nudged reconcile, and an idle_timeout_s change re-evaluates quiescence (notably,
    # dropping it to 0 resumes already-idle servers).
    if changes.keys() & {"docker_runner", "idle_timeout_s"}:
        request.app.state.supervisor.nudge()
    # A default-auth change must converge the group hub BEFORE this returns, not on the
    # next reconcile: every /g dispatcher enforces the NEW default auth on the very next
    # request, so a bearer->none downgrade must not leave a group's OLD mounted set
    # (which may include bearer-only members) serveable in the gap. sync() is
    # lock-serialized and its lifespans run in their own tasks, so calling it here is safe.
    return result
