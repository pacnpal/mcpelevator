"""Known-failure signatures -> operator hints.

A terminally failed activation usually surfaces as a generic error ("readiness
timed out", "bridge exited rc=1") while the real cause — often a well-understood
upstream breakage whose fix is one config edit away — sits in the log backlog.
This table maps log signatures to actionable recommendations; the unit appends
the first match to ``last_error``, so the hint reaches the API/UI through the
existing field with no schema change.

Keep entries here for failures that are (a) recognizable from a log line and
(b) fixable by the operator editing the server's config — not for bugs the
operator can't act on.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

# The mcp 2.0 Python SDK moved/removed symbols (e.g. McpError left
# mcp.shared.exceptions). Servers declaring an open ``mcp>=…`` constraint
# resolve 2.x on a cold uvx run and die at import with exactly this shape.
_MCP2_IMPORT = re.compile(r"cannot import name '[^']+' from 'mcp[.']")


def _mcp2_hint(runner: str) -> str:
    cause = (
        "the upstream server crashed importing the Python mcp SDK — "
        "it looks incompatible with the mcp 2.x line"
    )
    if runner == "uvx":
        return (
            f'{cause}. Add --with "mcp<2" before the package name in the '
            "server's arguments to pin the 1.x SDK until upstream ships a fix"
        )
    return (
        f"{cause}. Pin mcp<2 in the server's Python environment until "
        "upstream ships a fix"
    )


# (signature, runner -> hint) — first match over the log tail wins.
_SIGNATURES: list[tuple[re.Pattern[str], Callable[[str], str]]] = [
    (_MCP2_IMPORT, _mcp2_hint),
]

# Failures repeat per attempt, so the signature is always near the end; a
# bounded tail keeps the scan cheap even against a full 2000-line buffer.
_TAIL_LINES = 400


def startup_hint(lines: Iterable[str], runner: str) -> Optional[str]:
    """Recommendation for a terminally failed activation, from its log tail.

    Returns ``None`` when nothing recognizable matched — the common case."""
    tail = list(lines)[-_TAIL_LINES:]
    for pattern, hint in _SIGNATURES:
        if any(pattern.search(line) for line in tail):
            return hint(runner)
    return None
