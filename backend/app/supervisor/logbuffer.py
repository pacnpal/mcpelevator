"""In-memory ring buffer of a server's bridge-host log lines.

Holds a bounded backlog (for the UI to show on open) and supports live
subscribers (for the SSE tail endpoint added in M2). All access happens on the
event-loop thread (the log pump and the API run on the same loop).
"""

from __future__ import annotations

import asyncio
from collections import deque


class LogBuffer:
    def __init__(self, maxlen: int = 2000):
        self.lines: deque[str] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue[str]] = set()

    def append(self, line: str) -> None:
        self.lines.append(line)
        for q in list(self._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> list[str]:
        return list(self.lines)

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)
