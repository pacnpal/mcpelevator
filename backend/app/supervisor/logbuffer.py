"""In-memory ring buffer for one Server activation.

Holds setup, bridge, readiness, and retry output in one bounded backlog and
supports live subscribers. All access happens on the event-loop thread.
"""

from __future__ import annotations

import asyncio
from collections import deque


class LogBuffer:
    def __init__(self, maxlen: int = 2000):
        self.lines: deque[str] = deque(maxlen=maxlen)
        self.dropped = 0
        self._subscribers: set[asyncio.Queue[str]] = set()

    def append(self, line: str) -> None:
        if self.lines.maxlen is not None and len(self.lines) == self.lines.maxlen:
            self.dropped += 1
        self.lines.append(line)
        for q in list(self._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> list[str]:
        lines = list(self.lines)
        if self.dropped:
            lines.insert(
                0,
                f"[mcpelevator] {self.dropped} earlier log line(s) omitted (buffer limit)",
            )
        return lines

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)
