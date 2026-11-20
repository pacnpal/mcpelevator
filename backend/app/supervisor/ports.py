"""Loopback port allocation for bridge-host processes.

Ports are *runtime*, never identity: we allocate from a bounded range and hand out
the listening socket itself, which the unit passes to its bridge as an open file
descriptor (issue #302). The port is held from the moment it is chosen until the
unit releases it, so nothing else can bind it in between and a retry reuses the
same socket. A hard cap (max_running) prevents port exhaustion / runaway process
counts.
"""

from __future__ import annotations

import socket


def listen(host: str, port: int) -> socket.socket:
    """A TCP socket bound to ``host:port`` and listening; raises OSError when taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


class PortAllocator:
    def __init__(self, start: int, end: int, host: str = "127.0.0.1"):
        self.start = start
        self.end = end
        self.host = host
        self._used: set[int] = set()

    def allocate(self) -> socket.socket:
        for port in range(self.start, self.end):
            if port in self._used:
                continue
            try:
                sock = listen(self.host, port)
            except OSError:
                continue
            self._used.add(port)
            return sock
        raise RuntimeError(f"no free port in range {self.start}-{self.end}")

    def release(self, port: int) -> None:
        self._used.discard(port)
