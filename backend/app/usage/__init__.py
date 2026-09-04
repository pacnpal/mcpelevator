"""Usage accounting: how much each server — and each of its tools — is actually used.

Three small pieces, kept apart so each is testable on its own:

- ``attribution`` — pure rules mapping a data-plane request to the tool it invoked.
- ``recorder`` — the in-memory counter and its periodic flush into hourly buckets.
- ``stats`` — the read side, folding stored buckets into a chartable shape.

Counts only: a usage row holds a server id, a tool name, an hour and a number.
Tool arguments and results are never inspected or stored.
"""

from app.usage.attribution import NOT_A_TOOL, proxy_tools, split_namespaced, tools_from_body
from app.usage.recorder import flush, flush_sync, record, run_forever
from app.usage.stats import MAX_DAYS, server_usage

__all__ = [
    "MAX_DAYS",
    "NOT_A_TOOL",
    "flush",
    "flush_sync",
    "proxy_tools",
    "record",
    "run_forever",
    "server_usage",
    "split_namespaced",
    "tools_from_body",
]
