"""Group registry endpoints: read, create/replace, and delete the named groups
served at ``/g/<name>/mcp``.

The registry is the single source of truth (``app.groups.registry``, backed by the
``groups`` runtime setting). A write is referentially validated (every member must
be a registered server) before it lands, and the group hub is resynced before the
call returns so a membership change takes effect immediately rather than on the next
reconcile — the same "no async gap" contract the server endpoints use so a stale
mounted set is never serveable before the reconciler fires.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import Response

from app.api.schemas import GroupInfo, GroupUpsert
from app.api.util import base_url, resync_groups
from app.auth import principal as principal_mod
from app.auth.principal import Principal, require_admin
from app.db import get_session, repo
from app.groups import registry
from app.registry import service

# Groups are a global, admin-owned surface: they can bundle ANY server (including
# "*" = everything), so members must not read or shape them.
router = APIRouter(dependencies=[Depends(require_admin)])


def _url(request: Request, name: str) -> str:
    return f"{base_url(request)}/g/{name}/mcp"


_FORBIDDEN = HTTPException(status_code=403, detail="admin role required")


def _delete_group_and_tokens(session: Session, name: str, principal: Principal) -> bool:
    """Revoke the group's tokens and remove its registry entry under a SINGLE hold of the
    config write lock. Run in the threadpool by the caller: the lock is a threading
    RLock, so waiting on it (while a server import/create holds it deriving config hashes)
    must not sit on the event loop.

    Holding the lock across the whole operation makes it atomic w.r.t. group-token
    creation (which takes the same lock and rechecks existence before inserting), closing
    the race where a token minted for a group being deleted would survive the revocation
    and re-authorize a same-named group recreated later. Revoke BEFORE removing so an
    interruption leaves the benign state (tokens gone, group lingers). Returns False when
    the group didn't exist (-> 404)."""
    with service.config_write_lock():
        # Re-authorize under the lock: a demotion of the caller can commit while
        # this request queued behind an import — the entry-time require_admin must
        # not let a now-member delete a global group and revoke its tokens.
        if not principal_mod.admin_now(session, principal):
            raise _FORBIDDEN
        if not registry.exists(session, name):
            return False
        repo.delete_tokens_by_scope(session, f"group:{name}")
        registry.delete_group(session, name)
        return True


@router.get("/groups", response_model=list[GroupInfo])
async def list_groups(request: Request, session: Session = Depends(get_session)):
    return [
        GroupInfo(name=name, members=members, url=_url(request, name))
        for name, members in registry.read(session).items()
    ]


@router.put("/groups/{name}", response_model=GroupInfo)
async def upsert_group(
    name: str,
    payload: GroupUpsert,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    def _write():
        # One lock hold (reentrant — write_group re-enters it): re-authorize the
        # caller on committed state, then validate-and-write the registry entry.
        with service.config_write_lock():
            if not principal_mod.admin_now(session, principal):
                raise _FORBIDDEN
            return registry.write_group(session, name, payload.members)

    try:
        # Threadpool: the config write lock is shared with server creates/imports
        # deriving scrypt hashes, so the wait must stay off the loop.
        stored = await run_in_threadpool(_write)
    except ValueError as exc:  # bad name grammar or an unknown member id
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await resync_groups(request)
    return GroupInfo(name=name, members=stored[name], url=_url(request, name))


@router.delete("/groups/{name}", status_code=204)
async def delete_group(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
):
    # Revoke + remove under the config write lock, off the event loop (see helper).
    if not await run_in_threadpool(_delete_group_and_tokens, session, name, principal):
        raise HTTPException(status_code=404, detail="group not found")
    await resync_groups(request)
    return Response(status_code=204)
