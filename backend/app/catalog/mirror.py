"""Local mirror of each catalog directory, so browsing survives an upstream outage.

The MCP Registry promises no uptime and asks consumers to sync about hourly and keep
their own copy; Glama's API is built for the same pattern. So a background task pages
through each source (``Source.mirror_pages``) on its ``mirror_interval`` and stores the
raw records here. Once a source has finished one full sync, its list view and its
detail view read from this copy, and the live API is only a fallback.

This is a cache, not desired state: it lives in its own SQLite file next to the main
database, holds nothing an operator wrote, and can be deleted at any time (the next
sync rebuilds it). The stdlib ``sqlite3`` is enough; every call is short and runs in a
worker thread so the event loop never waits on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx

from app.catalog.base import CatalogUpstreamError, MirrorRow, Source
from app.config import get_settings

logger = logging.getLogger(__name__)

CURSOR_PREFIX = "m:"  # marks an offset cursor into the mirror, vs an upstream cursor
_TICK_S = 60.0
_RETRY_S = 300.0  # after a failed sync; the interval would wait too long
_OVERLAP_S = 300.0  # an incremental sync asks from a bit before the last one started

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    raw TEXT NOT NULL,
    gen INTEGER NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS entry_order ON entry (source, rank, id);
CREATE TABLE IF NOT EXISTS sync (
    source TEXT PRIMARY KEY,
    started REAL NOT NULL,
    finished REAL NOT NULL
);
"""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """One short-lived connection, committed on success and always closed."""
    path = get_settings().data_dir / "catalog-mirror.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # reads keep working during a sync's writes
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _ready(source: str) -> bool:
    with _db() as conn:
        return conn.execute("SELECT 1 FROM sync WHERE source = ?", (source,)).fetchone() is not None


def _last_started(source: str) -> float | None:
    with _db() as conn:
        row = conn.execute("SELECT started FROM sync WHERE source = ?", (source,)).fetchone()
    return row[0] if row else None


def _like(term: str) -> str:
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _query(source: str, search: str | None, offset: int, limit: int) -> tuple[list[dict], bool]:
    sql = "SELECT raw FROM entry WHERE source = ? AND status != 'deleted'"
    args: list[Any] = [source]
    for term in (search or "").split():
        sql += " AND text LIKE ? ESCAPE '\\'"
        args.append(_like(term))
    sql += " ORDER BY rank, id LIMIT ? OFFSET ?"
    args += [limit + 1, offset]
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [json.loads(r[0]) for r in rows[:limit]], len(rows) > limit


def _get(source: str, id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT raw FROM entry WHERE source = ? AND id = ?", (source, id)).fetchone()
    return json.loads(row[0]) if row else None


def _write(source: str, rows: list[MirrorRow], gen: int, rank: int | None) -> None:
    """Upsert one page. ``rank`` is the page's starting position on a full sync; on an
    incremental one it's None and new rows go to the end while known rows keep theirs."""
    with _db() as conn:
        if rank is None:
            (rank,) = conn.execute(
                "SELECT COALESCE(MAX(rank) + 1, 0) FROM entry WHERE source = ?", (source,)
            ).fetchone()
            keep = "rank"
        else:
            keep = "excluded.rank"
        conn.executemany(
            "INSERT INTO entry (source, id, rank, status, text, raw, gen) VALUES (?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT (source, id) DO UPDATE SET rank = {keep}, status = excluded.status, "
            "text = excluded.text, raw = excluded.raw, gen = excluded.gen",
            [
                (source, r.id, rank + i, r.status, r.text.lower(), json.dumps(r.raw), gen)
                for i, r in enumerate(rows)
            ],
        )


def _finish(source: str, started: float, full: bool, gen: int) -> None:
    with _db() as conn:
        if full:  # anything the full listing didn't return is gone upstream
            conn.execute("DELETE FROM entry WHERE source = ? AND gen < ?", (source, gen))
        conn.execute(
            "INSERT INTO sync (source, started, finished) VALUES (?, ?, ?) "
            "ON CONFLICT (source) DO UPDATE SET started = excluded.started, finished = excluded.finished",
            (source, started, time.time()),
        )


async def list_page(
    source: str, search: str | None, cursor: str | None, limit: int
) -> tuple[list[dict], str | None] | None:
    """A page of raw records from the mirror, with the next cursor, or None when the
    live API should answer instead: the mirror hasn't finished its first sync, or the
    caller is partway through paging an upstream cursor, or mirroring is off (a copy
    left from before it was turned off would otherwise go stale forever)."""
    if not get_settings().catalog_mirror or (cursor and not cursor.startswith(CURSOR_PREFIX)):
        return None
    try:
        offset = int(cursor[len(CURSOR_PREFIX):]) if cursor else 0
    except ValueError:
        offset = 0
    if not await asyncio.to_thread(_ready, source):
        return None
    raws, more = await asyncio.to_thread(_query, source, search, offset, limit)
    return raws, (f"{CURSOR_PREFIX}{offset + limit}" if more else None)


async def get(source: str, id: str) -> dict | None:
    """The mirrored raw record for ``id``, if mirroring is on and the mirror has it."""
    if not get_settings().catalog_mirror:
        return None
    return await asyncio.to_thread(_get, source, id)


async def sync(src: Source, http: httpx.AsyncClient) -> int:
    """Run one sync of ``src`` and return how many records it wrote. Incremental when
    the source supports it and a sync has finished before; otherwise a full pass that
    also drops records the upstream no longer lists."""
    started = time.time()
    gen = int(started * 1000)
    last = await asyncio.to_thread(_last_started, src.id)
    full = last is None or not getattr(src, "mirror_incremental", False)
    since = None
    if not full:
        since = datetime.fromtimestamp(last - _OVERLAP_S, UTC).isoformat().replace("+00:00", "Z")
    count = 0
    async for rows in src.mirror_pages(http, since=since):
        if rows:
            await asyncio.to_thread(_write, src.id, rows, gen, count if full else None)
            count += len(rows)
    await asyncio.to_thread(_finish, src.id, started, full, gen)
    return count


async def run_forever(sources: list[Source], http: httpx.AsyncClient) -> None:
    """Keep every mirrored source within its interval until cancelled. A failed sync
    is logged and retried a few minutes later; the last good copy keeps serving."""
    failed_at: dict[str, float] = {}
    while True:
        for src in sources:
            interval = src.mirror_interval()
            if interval is None:
                continue
            now = time.time()
            last = await asyncio.to_thread(_last_started, src.id)
            if last is not None and now - last < interval:
                continue
            if now - failed_at.get(src.id, 0.0) < _RETRY_S:
                continue
            try:
                count = await sync(src, http)
                failed_at.pop(src.id, None)
                logger.info("catalog mirror: synced %s records from %s", count, src.label)
            except asyncio.CancelledError:
                raise
            except (CatalogUpstreamError, httpx.HTTPError) as exc:
                failed_at[src.id] = now
                logger.warning("catalog mirror: %s sync failed, retrying later: %s", src.label, exc)
            except Exception:
                failed_at[src.id] = now
                logger.exception("catalog mirror: %s sync failed", src.label)
        await asyncio.sleep(_TICK_S)
