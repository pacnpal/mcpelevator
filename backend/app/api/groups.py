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
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import Response

from app.api.schemas import GroupInfo, GroupUpsert
from app.api.util import base_url
from app.db import get_session, repo
from app.groups import registry

router = APIRouter()


def _url(request: Request, name: str) -> str:
    return f"{base_url(request)}/g/{name}/mcp"


async def _resync_groups(request: Request) -> None:
    """Converge the group hub NOW instead of on the next reconcile, so a just-written
    (or deleted) group serves its new membership immediately. No-op when a group's
    topology key is unchanged; sync() is lock-serialized and task-safe."""
    try:
        await request.app.state.groups.sync(request.app.state.supervisor)
    except Exception as exc:  # the registry write already committed; don't fail the call
        print(f"[mcpelevator] group resync error: {exc}", flush=True)


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
):
    try:
        registry.write_group(session, name, payload.members)
    except ValueError as exc:  # bad name grammar or an unknown member id
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _resync_groups(request)
    stored = registry.read(session)[name]
    return GroupInfo(name=name, members=stored, url=_url(request, name))


@router.delete("/groups/{name}", status_code=204)
async def delete_group(
    name: str, request: Request, session: Session = Depends(get_session)
):
    if not registry.delete_group(session, name):
        raise HTTPException(status_code=404, detail="group not found")
    # Revoke tokens scoped to this group. Its scope string (``group:<name>``) is
    # deterministic, so a lingering token would silently re-authorize a *different*
    # group later recreated under the same name — unlike a random, never-reused server id.
    repo.delete_tokens_by_scope(session, f"group:{name}")
    await _resync_groups(request)
    return Response(status_code=204)
