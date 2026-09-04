"""Read side: fold stored buckets into the shapes a dashboard needs.

Two views, one set of rules: :func:`server_usage` for a single server and
:func:`instance_usage` across every server a principal can see. Both derive the
window, roll up to the same bucket width and densify the same way, so "the last
7 days" can't mean two different things depending on which page you opened.

Rollups happen in SQL (``repo.usage_series`` / ``usage_by_server`` /
``usage_by_tool``): an instance-wide view spans every server, and folding raw
buckets in Python would scale with servers x tools x hours.

The server does the shaping so the SPA renders what it is handed — filtering and
sorting a handed-over list is the browser's job, deciding what the numbers MEAN
is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlmodel import Session

from app.db import repo
from app.db.models import Server, utcnow
from app.registry import settings as runtime_settings

HOUR_S = 3600
DAY_S = 86400
# Above this many days an hourly series is more bars than a chart can show (and
# more rows than the answer needs), so the window rolls up to whole days.
HOURLY_MAX_DAYS = 2
MAX_DAYS = 365
# Servers that get their own band in the split-by-server view before the tail
# folds into "Other". A cap keeps the payload bounded and the facets readable; the
# full listing is always available in `servers`.
SPLIT_SERIES_LIMIT = 5


def _as_utc(value: datetime) -> datetime:
    """SQLite hands datetimes back naive; every stored value is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _floor(value: datetime, step_s: int) -> datetime:
    """Snap a time down to the start of its `step_s` bucket, in UTC.

    Floors on the epoch second rather than by replacing calendar fields, so it
    works for any step (hour or day) with one expression and lands on the same
    boundaries the stored buckets use."""
    epoch_s = int(value.timestamp())
    return datetime.fromtimestamp(epoch_s - epoch_s % step_s, tz=timezone.utc)


def _window(days: int, *, retention_days: int = 0) -> tuple[datetime, int, int]:
    """``(since, bucket_width, point_count)`` for a trailing window of ``days``.
    The last point is the current, still-filling bucket.

    The window is CLAMPED to retention when one is set (``0`` = keep forever).
    Past the retention cutoff the buckets were deleted, so reporting them as dense
    zeroes would draw discarded history as genuine quiet — asking for a year on a
    30-day retention must return 30 days, and say so through ``since``."""
    days = max(1, min(days, MAX_DAYS))
    if retention_days > 0:
        days = min(days, retention_days)
    step_s = HOUR_S if days <= HOURLY_MAX_DAYS else DAY_S
    points = days * 24 if step_s == HOUR_S else days
    end = _floor(utcnow(), step_s)
    return end - timedelta(seconds=step_s * (points - 1)), step_s, points


def _densify(
    rows: Iterable[tuple[datetime, int, int]], since: datetime, step_s: int, points: int
) -> list[dict[str, Any]]:
    """Hourly rows -> a DENSE series at the window's bucket width. Quiet buckets are
    present with zeroes so a chart can render the window without filling gaps
    itself, and rows outside the window are dropped rather than clamped into an
    edge bucket (which would show traffic in an hour that had none)."""
    series: dict[datetime, list[int]] = {
        since + timedelta(seconds=step_s * i): [0, 0] for i in range(points)
    }
    for bucket, calls, other in rows:
        point = series.get(_floor(_as_utc(bucket), step_s))
        if point is not None:
            point[0] += calls
            point[1] += other
    return [
        {"bucket": bucket, "calls": counts[0], "other": counts[1]}
        for bucket, counts in sorted(series.items())
    ]


def _bucket_index(
    bucket: datetime, since: datetime, step_s: int, points: int
) -> Optional[int]:
    """Where a stored hour lands in the dense series, or None when outside it."""
    offset = int((_floor(_as_utc(bucket), step_s) - since).total_seconds()) // step_s
    return offset if 0 <= offset < points else None


def _split_by_server(
    session: Session,
    servers: Sequence[Server],
    ranked: Sequence[dict[str, Any]],
    *,
    since: datetime,
    step_s: int,
    points: int,
) -> list[dict[str, Any]]:
    """Per-server call series, aligned index-for-index with the dense series.

    Only the busiest :data:`SPLIT_SERIES_LIMIT` servers get their own band; the
    rest fold into one "Other". Folding HERE rather than in the browser keeps the
    payload bounded and the decision in one place — the full per-server listing is
    always in ``servers``, so nothing is hidden by the cap.

    Timestamps aren't repeated: each band is a plain list of counts positioned by
    the series it belongs to."""
    named = [row["server_id"] for row in ranked[:SPLIT_SERIES_LIMIT] if row["tool_calls"] > 0]
    slot = {server_id: i for i, server_id in enumerate(named)}
    bands = [[0] * points for _ in range(len(named) + 1)]  # +1: the folded remainder
    scope = [server.id for server in servers]

    used_other = False
    for server_id, bucket, calls in repo.usage_series_by_server(
        session, since=since, server_ids=scope
    ):
        index = _bucket_index(bucket, since, step_s, points)
        if index is None:
            continue
        band = slot.get(server_id)
        if band is None:
            band, used_other = len(named), True
        bands[band][index] += calls

    by_id = {server.id: server for server in servers}
    out = [
        {
            "server_id": server_id,
            "slug": by_id[server_id].slug,
            "name": by_id[server_id].name,
            "points": bands[i],
        }
        for i, server_id in enumerate(named)
        if server_id in by_id
    ]
    if used_other:
        out.append({"server_id": None, "slug": "other", "name": "Other", "points": bands[-1]})
    return out


def _newest(current: Optional[datetime], candidate: Optional[datetime]) -> Optional[datetime]:
    """Fold one more `last_call_at` into a running maximum, `None` meaning absent.

    Normalizes through `_as_utc` first: SQLite hands datetimes back naive, so
    comparing a fresh read against an already-normalized aware value would raise
    rather than order."""
    if candidate is None:
        return current
    candidate = _as_utc(candidate)
    return candidate if current is None or candidate > current else current


def server_usage(session: Session, server_id: str, *, days: int) -> dict[str, Any]:
    """Usage for one server over the trailing ``days``.

    The window reaches back only as far as the ``usage_retention_days`` setting
    has kept rows — asking for more returns the shorter window, not zeroes."""
    since, step_s, points = _window(
        days, retention_days=runtime_settings.usage_retention_days(session)
    )
    scope = [server_id]

    tool_calls = other_requests = 0
    last_call_at: Optional[datetime] = None
    for _, calls, other, last in repo.usage_by_server(session, since=since, server_ids=scope):
        tool_calls += calls
        other_requests += other
        last_call_at = _newest(last_call_at, last)

    tools = [
        {"tool": tool, "calls": calls, "last_call_at": _as_utc(last)}
        for _, tool, calls, last in repo.usage_by_tool(session, since=since, server_ids=scope)
    ]

    return {
        "since": since,
        "bucket_seconds": step_s,
        "tool_calls": tool_calls,
        "other_requests": other_requests,
        "last_call_at": last_call_at,
        # Busiest first, then alphabetical — a stable order for a table the
        # operator reads top-down looking for the tool nothing ever calls.
        "tools": sorted(tools, key=lambda t: (-t["calls"], t["tool"])),
        "series": _densify(
            repo.usage_series(session, since=since, server_ids=scope), since, step_s, points
        ),
    }


def _known_tools(session: Session, server_ids: set[str]) -> dict[str, list[str]]:
    """Each server's currently discovered tool names, by server id.

    Read from the persisted runtime rows, which cache what the readiness probe
    last saw under the names clients actually call (post-rename) — the same key
    usage is recorded under. A server that isn't running has no cached tools, so
    it contributes none; its previously called tools still appear from the
    counters themselves."""
    known: dict[str, list[str]] = {}
    for runtime in repo.list_runtimes(session):
        if runtime.server_id not in server_ids:
            continue
        names = [
            tool.get("name")
            for tool in (runtime.tools or [])
            if isinstance(tool, dict) and tool.get("name")
        ]
        known[runtime.server_id] = names
    return known


def instance_usage(session: Session, servers: Sequence[Server], *, days: int) -> dict[str, Any]:
    """Usage across the servers a principal can see.

    Every one of them appears in ``servers`` — including the untouched ones,
    because "which of my servers is nothing using?" is half the question this
    view answers. Likewise ``tools`` carries a zero row for every discovered tool
    nothing called, and a row for a tool called under a name its server no longer
    exposes (``known: False``), so neither disappears from the listing.

    The window is clamped to retention exactly as :func:`server_usage` clamps it."""
    since, step_s, points = _window(
        days, retention_days=runtime_settings.usage_retention_days(session)
    )
    scope = [server.id for server in servers]

    by_server = {
        server_id: (calls, other, last)
        for server_id, calls, other, last in repo.usage_by_server(
            session, since=since, server_ids=scope
        )
    }
    called: dict[str, dict[str, tuple[int, datetime]]] = {}
    for server_id, tool, calls, last in repo.usage_by_tool(
        session, since=since, server_ids=scope
    ):
        called.setdefault(server_id, {})[tool] = (calls, _as_utc(last))
    known = _known_tools(session, set(scope))

    server_rows: list[dict[str, Any]] = []
    tool_rows: list[dict[str, Any]] = []
    tool_calls = other_requests = active_servers = 0
    last_call_at: Optional[datetime] = None

    for server in servers:
        calls, other, last = by_server.get(server.id, (0, 0, None))
        tool_calls += calls
        other_requests += other
        last_call_at = _newest(last_call_at, last)
        if calls or other:
            active_servers += 1

        server_called = called.get(server.id, {})
        server_known = known.get(server.id, [])
        for tool in sorted(set(server_known) | set(server_called)):
            hit = server_called.get(tool)
            tool_rows.append(
                {
                    "server_id": server.id,
                    "slug": server.slug,
                    "tool": tool,
                    "calls": hit[0] if hit else 0,
                    "last_call_at": hit[1] if hit else None,
                    "known": tool in server_known,
                }
            )
        server_rows.append(
            {
                "server_id": server.id,
                "slug": server.slug,
                "name": server.name,
                "tool_calls": calls,
                "other_requests": other,
                "last_call_at": _as_utc(last) if last is not None else None,
                # Counted against what the server exposes RIGHT NOW, so the pair
                # reads as a ratio ("3/8 tools used"). A name that was called and
                # has since been renamed, hidden, or dropped upstream stays in
                # the per-tool listing above (marked "retired") but is not in
                # `known` — counting it here would render "2/1 tools used" the
                # moment both sides of a rename saw traffic in the window.
                "tools_called": len(set(server_called) & set(server_known)),
                "tools_known": len(server_known),
            }
        )

    ranked = sorted(server_rows, key=lambda s: (-s["tool_calls"], s["name"].lower()))
    hourly = repo.usage_series(session, since=since, server_ids=scope)
    return {
        "since": since,
        "bucket_seconds": step_s,
        "tool_calls": tool_calls,
        "other_requests": other_requests,
        "last_call_at": last_call_at,
        "active_servers": active_servers,
        "servers": ranked,
        "tools": sorted(tool_rows, key=lambda t: (-t["calls"], t["slug"], t["tool"])),
        "series": _densify(hourly, since, step_s, points),
        "series_by_server": _split_by_server(
            session, servers, ranked, since=since, step_s=step_s, points=points
        ),
        # Always hourly and SPARSE (quiet hours are simply absent), whatever width
        # the main series was rolled up to: an activity-by-hour view needs the hour
        # back, and only the browser knows the reader's timezone to bucket it in.
        "hourly": [
            {"bucket": _as_utc(bucket), "calls": calls}
            for bucket, calls, _other in hourly
            if calls
        ],
    }
