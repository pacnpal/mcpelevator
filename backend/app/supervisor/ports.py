"""Loopback port allocation for bridge-host processes.

Ports are *runtime*, never identity: we allocate from a bounded range, OS-probe
for freeness, and on collision the child fails fast and the reconciler retries.
A hard cap (max_running) prevents port exhaustion / runaway process counts.
"""

from __future__ import annotations

import socket


class PortAllocator:
    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end
        self._used: set[int] = set()

    def _free_on_os(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def allocate(self) -> int:
        for port in range(self.start, self.end):
            if port in self._used:
                continue
            if self._free_on_os(port):
                self._used.add(port)
                return port
        raise RuntimeError(f"no free port in range {self.start}-{self.end}")

    def release(self, port: int) -> None:
        self._used.discard(port)
