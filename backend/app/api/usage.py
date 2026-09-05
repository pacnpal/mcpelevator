"""Instance-wide usage: the dashboard's read model.

Per-server usage lives with the other ``/servers/{id}`` routes; this is the view
ACROSS servers, so it gets its own router. Visibility is the policy module's
call, exactly as everywhere else: an admin sees every server, a member sees the
ones they own, and a member's totals are the sum over those alone — never a
whole-instance number leaked through an aggregate.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session

from app import usage
from app.api.schemas import InstanceUsage
from app.auth import policy
from app.auth import principal as principal_mod
from app.auth.principal import Principal, current_principal
from app.db import get_engine, repo

router = APIRouter()


@router.get("/usage", response_model=InstanceUsage)
async def get_instance_usage(
    response: Response,
    days: int = Query(default=7, ge=1, le=usage.MAX_DAYS),
    principal: Principal = Depends(current_principal),
):
    """Totals, a series to chart, and per-server / per-tool rollups.

    Pending counts are flushed first so a call made seconds ago is already
    visible — a dashboard that lags its own traffic by a flush interval reads as
    broken, and this is a rare, operator-driven read.

    Both the flush and the AGGREGATION run off the event loop. The rollups are
    several synchronous SQLite scans whose cost grows with servers x tools x
    hours, and this process serves `/s` proxy traffic and runs supervision on
    that same loop — one dashboard request must not stall them. The worker owns
    its own session: a session made on the loop is not the thread's to use.

    Authority is RE-READ in that worker, not taken from the entry-time principal:
    the flush is awaited first, and an admin can demote the caller or revoke the
    very token this request authenticated with while it waits. This body is the
    whole instance's server names and tool usage to an admin, so it fails closed
    on a principal that no longer resolves — the same rule every other decision
    made after an await follows (`principal.refresh`).

    `no-store` because the body is scoped to WHO asked: the totals are summed
    over the caller's visible servers alone, so a cached copy could be replayed
    to a different principal on a shared browser or by an intermediary."""

    def _compute() -> dict:
        with Session(get_engine()) as session:
            fresh = principal_mod.refresh(session, principal)
            if fresh is None:
                raise HTTPException(
                    status_code=401,
                    detail="control-plane auth required",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            servers = policy.visible_servers(fresh, repo.list_servers(session))
            return usage.instance_usage(session, servers, days=days)

    await usage.flush()
    payload = await asyncio.to_thread(_compute)
    response.headers["Cache-Control"] = "no-store"
    return InstanceUsage(**payload)
