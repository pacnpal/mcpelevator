"""Read side: fold stored buckets into the shape a chart needs.

The server does the shaping (dense series, totals, per-tool rollup) so the SPA
renders what it is handed instead of re-deriving windows in the browser — one
place decides what "the last 7 days" means.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session

from app.db import repo
from app.db.models import utcnow
from app.usage.attribution import NOT_A_TOOL

HOUR_S = 3600
DAY_S = 86400
# Above this many days an hourly series is more bars than a chart can show (and
# more rows than the answer needs), so the window rolls up to whole days.
HOURLY_MAX_DAYS = 2
MAX_DAYS = 365


def _as_utc(value: datetime) -> datetime:
    """SQLite hands datetimes back naive; every stored value is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _floor(value: datetime, step_s: int) -> datetime:
    epoch_s = int(value.timestamp())
    return datetime.fromtimestamp(epoch_s - epoch_s % step_s, tz=timezone.utc)


def server_usage(session: Session, server_id: str, *, days: int) -> dict[str, Any]:
    """Usage for one server over the trailing ``days``, ready for the API schema.

    The series is DENSE (quiet buckets are present with zeroes) so a chart can
    render it directly, and it ends with the current, still-filling bucket. The
    window reaches only as far back as the ``usage_retention_days`` setting has
    kept rows."""
    days = max(1, min(days, MAX_DAYS))
    step_s = HOUR_S if days <= HOURLY_MAX_DAYS else DAY_S
    points = days * 24 if step_s == HOUR_S else days
    now = utcnow()
    end = _floor(now, step_s)
    since = end - timedelta(seconds=step_s * (points - 1))

    series = {since + timedelta(seconds=step_s * i): [0, 0] for i in range(points)}
    tools: dict[str, dict[str, Any]] = {}
    tool_calls = 0
    other_requests = 0
    last_call_at: datetime | None = None

    for row in repo.usage_since(session, server_id, since):
        bucket = _floor(_as_utc(row.bucket), step_s)
        point = series.get(bucket)
        is_tool = row.tool != NOT_A_TOOL
        if point is not None:
            point[0 if is_tool else 1] += row.calls
        if is_tool:
            tool_calls += row.calls
            seen = _as_utc(row.last_call_at)
            entry = tools.get(row.tool)
            if entry is None:
                tools[row.tool] = {"tool": row.tool, "calls": row.calls, "last_call_at": seen}
            else:
                entry["calls"] += row.calls
                entry["last_call_at"] = max(entry["last_call_at"], seen)
            if last_call_at is None or seen > last_call_at:
                last_call_at = seen
        else:
            other_requests += row.calls

    return {
        "since": since,
        "bucket_seconds": step_s,
        "tool_calls": tool_calls,
        "other_requests": other_requests,
        "last_call_at": last_call_at,
        # Busiest first, then alphabetical — a stable order for a table the
        # operator reads top-down looking for the tool nothing ever calls.
        "tools": sorted(tools.values(), key=lambda t: (-t["calls"], t["tool"])),
        "series": [
            {"bucket": bucket, "calls": counts[0], "other": counts[1]}
            for bucket, counts in sorted(series.items())
        ],
    }
