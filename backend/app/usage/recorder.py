"""In-process usage accumulator and its periodic flush.

The data plane must not pay a SQLite write per request: a request handler only
increments an in-memory counter here (no I/O, no write lock, nothing that can
fail the request), and a background task folds the whole batch into the hourly
buckets every :data:`FLUSH_INTERVAL_S`. A crash loses at most one interval of
counters — the right trade for statistics, and the reason this is deliberately
NOT part of the request's transaction.

The same task carries retention: buckets older than the ``usage_retention_days``
setting are pruned hourly, so counters stay bounded without an operator ever
having to sweep them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from sqlmodel import Session

from app.db import get_engine, repo
from app.db.models import NOT_A_TOOL, utcnow
from app.registry import settings as runtime_settings

logger = logging.getLogger(__name__)

# How often pending counters are folded into SQLite.
FLUSH_INTERVAL_S = 15.0
# How often retention is applied (a delete per flush would be pure overhead).
PRUNE_INTERVAL_S = 3600.0
# Ceiling on DISTINCT (server, tool, hour) keys held between flushes. Tool names
# come from the caller, so on an open server a client can name tools that were
# never advertised — and each distinct name is a key here and a row in storage,
# which time-based retention does not bound. Past the ceiling, counts for keys
# not already being tracked are dropped (a flush clears it, and no legitimate
# instance mints thousands of distinct tool names in one interval).
MAX_PENDING_KEYS = 5000

# (server_id, tool, hour) -> calls. Guarded by _lock: increments run on the event
# loop, the flush swaps the dict, and the write itself happens in a worker thread.
_pending: defaultdict[tuple[str, str, datetime], int] = defaultdict(int)
# Keys detached by a flush whose write hasn't finished. They still count against
# MAX_PENDING_KEYS: a failed write merges them back, so without reserving them
# the map could hold up to twice the ceiling — the one case where the guard that
# exists to bound memory stops bounding it.
_inflight: dict[tuple[str, str, datetime], int] = {}
_lock = threading.Lock()
_last_prune: float | None = None


def _tracked_keys() -> int:
    """Distinct keys the recorder is holding: pending plus mid-flight. Call under
    `_lock`."""
    return len(_pending) + len(_inflight)


def current_bucket(now: datetime | None = None) -> datetime:
    """The UTC hour a call at ``now`` belongs to."""
    return (now or utcnow()).replace(minute=0, second=0, microsecond=0)


def record(server_id: str, tools: Iterable[str] = ()) -> None:
    """Count one dispatched data-plane request against a server.

    Each tool the request invoked is counted under its own name; a request that
    invoked none (``initialize``, ``tools/list``, an SSE GET) counts as plain
    traffic under the :data:`~app.db.models.NOT_A_TOOL` sentinel. Every
    request therefore contributes exactly one call — a batch, one per element —
    so a server's total is the sum of its rows.

    "Dispatched" is the honest boundary: it means the request reached a running
    bridge, whatever the tool then answered. An MCP tool failure is a successful
    HTTP call carrying ``isError``, and a request refused before a bridge was
    picked (unknown slug, auth, nothing running) never gets here at all."""
    names = [t for t in tools if t] or [NOT_A_TOOL]
    bucket = current_bucket()
    with _lock:
        for name in names:
            key = (server_id, name, bucket)
            # An already-tracked key always keeps counting; only NEW keys are
            # refused at the ceiling, so a flood of invented tool names can't
            # cost the real ones their counts. "Tracked" spans the in-flight
            # batch too — a key being written is still one this process holds.
            if (
                key not in _pending
                and key not in _inflight
                and _tracked_keys() >= MAX_PENDING_KEYS
            ):
                continue
            _pending[key] += 1


def _take() -> dict[tuple[str, str, datetime], int]:
    """Detach the whole pending batch under the lock, leaving `_pending` empty.

    Detaching rather than reading-then-clearing is what lets a request record a
    count while a flush is mid-write: the concurrent increment lands in the fresh
    map and belongs to the NEXT batch, so it can neither be double-written nor
    dropped when this one commits. A failed write hands the batch back via
    `_restore`; either way `_settle` releases the reservation."""
    with _lock:
        if not _pending:
            return {}
        batch = dict(_pending)
        _pending.clear()
        # Reserved, not released: these keys stay counted against the ceiling
        # until the write settles, so requests arriving mid-write can't fill the
        # map to the cap on top of a batch that may still come back.
        _inflight.update(batch)
    return batch


def _settle(batch: dict[tuple[str, str, datetime], int]) -> None:
    """Release a batch's reservation once its write has settled, either way.
    Call under `_lock`, or via `_restore` which takes it."""
    for key in batch:
        _inflight.pop(key, None)


def _restore(batch: dict[tuple[str, str, datetime], int]) -> None:
    """Merge a detached batch back after a failed write, so a transient database
    error costs a retry rather than the counts themselves. Merged (not assigned)
    because requests kept arriving while the write was in flight.

    A key whose reservation is gone is NOT restored: only `forget` removes one
    mid-write, and it means the server was deleted — putting those counts back
    would resurrect what the delete dropped."""
    with _lock:
        for key, calls in batch.items():
            if key not in _inflight:
                continue
            _pending[key] += calls
        # The reservation becomes a real pending entry — release it, or the key
        # would be counted twice against the ceiling.
        _settle(batch)


def forget(server_id: str) -> None:
    """Drop every pending count for a server. Called when one is deleted: the
    delete removes stored rows, and without this the next flush would write the
    interval's counts straight back as rows no server owns (SQLite foreign keys
    are off, so nothing else would stop it).

    Covers the in-flight batch too: a flush already mid-write would otherwise
    restore that server's counts on failure, resurrecting exactly what the
    delete removed."""
    with _lock:
        for key in [key for key in _pending if key[0] == server_id]:
            del _pending[key]
        for key in [key for key in _inflight if key[0] == server_id]:
            del _inflight[key]


def _prune_if_due(session: Session) -> None:
    """Drop rows past the retention window, at most once per `PRUNE_INTERVAL_S`.

    Rate-limited on a monotonic clock rather than run every flush: pruning is a
    range delete over the whole table, and at the flush cadence it would be
    almost entirely wasted work. Runs off the back of a flush instead of its own
    task so retention needs no second scheduler."""
    global _last_prune
    now = time.monotonic()
    if _last_prune is not None and now - _last_prune < PRUNE_INTERVAL_S:
        return
    _last_prune = now
    days = runtime_settings.usage_retention_days(session)
    if days <= 0:  # 0 = keep forever (the operator's explicit opt-out)
        return
    repo.prune_usage(session, utcnow() - timedelta(days=days))


def flush_sync() -> int:
    """Fold pending counters into SQLite and apply retention; returns the number
    of (server, tool, hour) rows written. Synchronous by design — the caller
    decides the thread (the background task hands it to a worker so the event
    loop never blocks on the write); tests call it directly."""
    batch = _take()
    with Session(get_engine()) as session:
        try:
            repo.bump_usage(session, batch)
        except Exception:
            # The write is the only step whose failure loses data — put the batch
            # back so the next tick retries it. A prune failure AFTER this point
            # is not restored: those counts are already stored.
            _restore(batch)
            raise
        # Written and committed — the reservation has done its job.
        with _lock:
            _settle(batch)
        _prune_if_due(session)
    return len(batch)


async def flush() -> int:
    """Await one flush, off the event loop. The write is blocking SQLite I/O, so
    it goes to a worker thread — a read endpoint that flushes before answering
    must not stall every other request on the loop while it commits."""
    return await asyncio.to_thread(flush_sync)


async def run_forever() -> None:
    """Flush on a fixed interval until cancelled. A failed flush is logged and
    retried on the next tick: usage bookkeeping must never take down the
    control plane, and the counters it couldn't write were already merged back
    into nothing — they are simply lost, like the interval a crash loses."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        try:
            await flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("usage flush failed")


def reset() -> None:
    """Drop pending counters, any in-flight reservation, and the prune clock
    (tests)."""
    global _last_prune
    with _lock:
        _pending.clear()
        _inflight.clear()
    _last_prune = None
