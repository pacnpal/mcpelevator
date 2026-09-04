"""Instance-wide usage: the dashboard's read model.

Per-server usage lives with the other ``/servers/{id}`` routes; this is the view
ACROSS servers, so it gets its own router. Visibility is the policy module's
call, exactly as everywhere else: an admin sees every server, a member sees the
ones they own, and a member's totals are the sum over those alone — never a
whole-instance number leaked through an aggregate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlmodel import Session

from app import usage
from app.api.schemas import InstanceUsage
from app.auth import policy
from app.auth.principal import Principal, current_principal
from app.db import get_session, repo

router = APIRouter()


@router.get("/usage", response_model=InstanceUsage)
async def get_instance_usage(
    response: Response,
    days: int = Query(default=7, ge=1, le=usage.MAX_DAYS),
    session: Session = Depends(get_session),
    principal: Principal = Depends(current_principal),
):
    """Totals, a series to chart, and per-server / per-tool rollups.

    Pending counts are flushed first so a call made seconds ago is already
    visible — a dashboard that lags its own traffic by a flush interval reads as
    broken, and this is a rare, operator-driven read.

    `no-store` because the body is scoped to WHO asked: the totals are summed
    over the caller's visible servers alone, so a cached copy could be replayed
    to a different principal on a shared browser or by an intermediary."""
    servers = policy.visible_servers(principal, repo.list_servers(session))
    await usage.flush()
    response.headers["Cache-Control"] = "no-store"
    return InstanceUsage(**usage.instance_usage(session, servers, days=days))
