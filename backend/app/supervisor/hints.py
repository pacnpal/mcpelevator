"""Known-failure signatures -> operator hints.

A terminally failed activation usually surfaces as a generic error ("readiness
timed out", "bridge exited rc=1") while the real cause — often a well-understood
upstream breakage whose fix is one config edit away — sits in the log backlog.
This table maps log signatures to actionable recommendations; the unit appends
the first match to ``last_error``, so the hint reaches the API/UI through the
existing field with no schema change.

Keep entries here for failures that are (a) recognizable from a log line and
(b) fixable by the operator editing the server's config — not for bugs the
operator can't act on. Signatures must carry real evidence of the specific
break: a pattern loose enough to match unrelated failures recommends a remedy
that can't work.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

# The mcp 2.0 Python SDK import break, matched by known removed-symbol/module
# pairs — NOT any ImportError under ``mcp.*``, which would also catch a server
# importing a too-new symbol from an old SDK (where a downgrade cannot help) or
# a plain typo. Extend the alternation as more removed pairs are confirmed.
_MCP2_IMPORT = re.compile(
    r"cannot import name '(?:McpError)' from 'mcp\.shared\.exceptions'"
)


def _mcp2_hint(runner: str, setup_failed: bool) -> str:
    cause = (
        "the upstream server crashed importing the Python mcp SDK — "
        "it looks incompatible with the mcp 2.x line"
    )
    if setup_failed:
        # The setup script runs in its own /bin/sh with the child env; a launch-argv
        # pin (uvx --with) can't reach it, so point at the script's own installs.
        return (
            f"{cause} while the setup script ran. Pin mcp<2 wherever the setup "
            "script installs the SDK until upstream ships a fix"
        )
    if runner == "uvx":
        return (
            f"{cause}. Enable the server's \"Pin mcp SDK < 2\" compatibility "
            "toggle (Edit server) to hold the 1.x line until upstream ships a fix"
        )
    if runner == "docker":
        # Only the image selects what's installed inside the container; a host-side
        # or env pin can't change its packages.
        return (
            f"{cause}. Use (or rebuild) an image that ships an mcp 1.x SDK, or one "
            "with a server updated for 2.x"
        )
    return (
        f"{cause}. Pin mcp<2 in the server's Python environment until upstream "
        "ships a fix"
    )


# (signature, (runner, setup_failed) -> hint) — first match over the log tail wins.
_SIGNATURES: list[tuple[re.Pattern[str], Callable[[str, bool], str]]] = [
    (_MCP2_IMPORT, _mcp2_hint),
]

# Failures repeat per attempt, so the signature is always near the end; a
# bounded tail keeps the scan cheap even against a full 2000-line buffer.
_TAIL_LINES = 400

# The unit's own phase markers (``ServerUnit`` logs one before each phase's
# output: "[mcpelevator] attempt N/M: setup|bridge|readiness"). They section the
# shared log so a signature is only credited to the phase that emitted it — a
# setup script that PRINTS the traceback but succeeds must not put a launch-argv
# remedy on an unrelated later failure, and vice versa.
_PHASE_MARKER = re.compile(
    r"^\[mcpelevator\] attempt (\d+)/\d+: (setup|bridge|readiness)\b"
)


def startup_hint(
    lines: Iterable[str], runner: str, *, setup_failed: bool = False
) -> Optional[str]:
    """Recommendation for a terminally failed activation, from its log tail.

    Only the FINAL attempt's lines count as evidence: an earlier retry may have
    failed on the signature while the terminal failure is something else — the
    hint must describe what actually killed the activation. ``setup_failed``
    marks a failure in the setup-script phase, whose environment is separate
    from the launch argv — remedies differ, and only log lines from the matching
    phase count. Lines with no preceding marker (a tail truncated mid-section,
    or logs from outside a phase) default to the launch side, so the setup
    remedy always rests on an explicit setup section. Returns ``None`` when
    nothing recognizable matched — the common case."""
    wanted = "setup" if setup_failed else "launch"
    phase = "launch"
    latest_attempt: Optional[int] = None
    relevant: list[str] = []
    for line in list(lines)[-_TAIL_LINES:]:
        marker = _PHASE_MARKER.match(line)
        if marker:
            attempt = int(marker.group(1))
            if latest_attempt is None or attempt > latest_attempt:
                latest_attempt = attempt
                relevant.clear()
            phase = "setup" if marker.group(2) == "setup" else "launch"
            continue
        if phase == wanted:
            relevant.append(line)
    for pattern, hint in _SIGNATURES:
        if any(pattern.search(line) for line in relevant):
            return hint(runner, setup_failed)
    return None
