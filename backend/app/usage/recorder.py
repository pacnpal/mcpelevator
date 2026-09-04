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
from app.db.models import utcnow
from app.registry import settings as runtime_settings
from app.usage.attribution import NOT_A_TOOL

logger = logging.getLogger(__name__)

# How often pending counters are folded into SQLite.
FLUSH_INTERVAL_S = 15.0
# How often retention is applied (a delete per flush would be pure overhead).
PRUNE_INTERVAL_S = 3600.0

# (server_id, tool, hour) -> calls. Guarded by _lock: increments run on the event
# loop, the flush swaps the dict, and the write itself happens in a worker thread.
_pending: defaultdict[tuple[str, str, datetime], int] = defaultdict(int)
_lock = threading.Lock()
_last_prune: float | None = None


def current_bucket(now: datetime | None = None) -> datetime:
    """The UTC hour a call at ``now`` belongs to."""
    return (now or utcnow()).replace(minute=0, second=0, microsecond=0)


def record(server_id: str, tools: Iterable[str] = ()) -> None:
    """Count one dispatched data-plane request against a server.

    Each tool the request invoked is counted under its own name; a request that
    invoked none (``initialize``, ``tools/list``, an SSE GET) counts as plain
    traffic under the :data:`~app.usage.attribution.NOT_A_TOOL` sentinel. Every
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
            _pending[(server_id, name, bucket)] += 1


def _take() -> dict[tuple[str, str, datetime], int]:
    with _lock:
        if not _pending:
            return {}
        batch = dict(_pending)
        _pending.clear()
    return batch


def _prune_if_due(session: Session) -> None:
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
        repo.bump_usage(session, batch)
        _prune_if_due(session)
    return len(batch)


async def flush() -> int:
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
    """Drop pending counters and the prune clock (tests)."""
    global _last_prune
    with _lock:
        _pending.clear()
    _last_prune = None
