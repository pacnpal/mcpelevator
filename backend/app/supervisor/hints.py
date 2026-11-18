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
from typing import Callable, Iterable, Optional, Sequence

from app.runners.uvx import is_uv_launcher, pin_insert_index

# The mcp 2.0 Python SDK import break, matched by known removed-symbol/module
# pairs — NOT any ImportError under ``mcp.*``, which would also catch a server
# importing a too-new symbol from an old SDK (where a downgrade cannot help) or
# a plain typo. Extend the alternation as more removed pairs are confirmed.
_MCP2_IMPORT = re.compile(
    r"cannot import name '(?:McpError)' from 'mcp\.shared\.exceptions'"
)


def _mcp2_hint(
    runner: str,
    setup_failed: bool,
    command: str,
    args: Sequence[str],
    pinned: bool,
    evidence: Sequence[str] = (),
) -> str:
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
        # The toggle is already on and the import STILL failed: recommending it
        # again is a dead end — the pin either isn't reaching the failing import
        # (a nested resolve) or 1.x doesn't fix this package either.
        if pinned:
            return (
                f'{cause}, even though the "Pin mcp SDK < 2" toggle is already on '
                "— the pin may not reach the failing import (a nested resolve), or "
                "the package may not work with the 1.x line either; check the logs "
                "for the resolved mcp version and report the issue upstream"
            )
        # Only recommend the toggle where the service would actually accept it —
        # the pin needs a certain placement (see registry._require_pinnable_uvx);
        # for other shapes, point at the manual/rewrite alternatives instead of
        # an action whose save is refused.
        if pin_insert_index(command, list(args)) is not None:
            return (
                f"{cause}. Enable the server's \"Pin mcp SDK < 2\" compatibility "
                "toggle (Edit server) to hold the 1.x line until upstream ships a fix"
            )
        if is_uv_launcher(command):
            # An unpinnable uv SHAPE: the argv can be rearranged to take the pin.
            return (
                f"{cause}. This launch command's shape can't take the automatic pin — "
                "add --with \"mcp<2\" after the run subcommand in the server's "
                "arguments, or rewrite the command as \"uvx <package>\", until "
                "upstream ships a fix"
            )
        # An arbitrary executable on a uvx-classified row: no uv/uvx argv advice
        # applies — only the environment that executable runs in can hold the pin.
        return (
            f"{cause}. Pin mcp<2 in the Python environment this command runs in "
            "until upstream ships a fix"
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


# Playwright launching a browser it never downloaded: its own error names the missing
# executable under the ms-playwright cache, and its banner tells you to run the install
# command on a line of its own (box-drawing borders, and the unit's "[health] " prefix
# on a health check's output, aside). A bare mention of the command inside other text
# is not evidence.
_PLAYWRIGHT_BROWSER = re.compile(
    r"Executable doesn't exist at \S*ms-playwright"
    r"|^(?:\[health\] )?[\s║|]*npx playwright install[\s║|]*$"
)
# The browser a missing executable belongs to (``ms-playwright/firefox-1488/...``); the
# install banner names none, and chromium covers its headless shell (and ffmpeg) too.
_PLAYWRIGHT_DIR = re.compile(r"ms-playwright/(firefox|webkit)-")


def _playwright_browser(evidence: Sequence[str]) -> str:
    for line in evidence:
        found = _PLAYWRIGHT_DIR.search(line)
        if found:
            return found.group(1)
    return "chromium"


def _playwright_hint(
    runner: str,
    setup_failed: bool,
    command: str,
    args: Sequence[str],
    pinned: bool,
    evidence: Sequence[str] = (),
) -> str:
    cause = "Playwright couldn't find its browser — it was never downloaded where this server runs"
    if runner == "docker":
        return (
            f"{cause}. Use (or build) an image that ships the Playwright browsers "
            "(e.g. one based on mcr.microsoft.com/playwright), or install them in its Dockerfile"
        )
    if runner not in ("npx", "uvx", "command"):
        return (
            f"{cause}: the upstream server is missing it, and it has to be installed "
            "where that server runs"
        )
    where = (
        "at the start of the setup script, before whatever ran Playwright"
        if setup_failed
        else "to the server's setup script (it runs before every start, with the "
        "server's environment, so the browser lands in the cache the server reads)"
    )
    browser = _playwright_browser(evidence)
    python = (
        " For a Python server, install with its Python Playwright package instead "
        f"(e.g. uvx playwright@<version> install {browser})."
        if runner == "uvx"
        else ""
    )
    return (
        f"{cause}. Add `npx playwright@<version> install {browser}` {where}, with "
        "<version> the Playwright version the server itself uses (another release "
        "downloads a different browser build than the one it looks for); if the browser "
        "then fails on missing system libraries, add them with MCPE_APT_PACKAGES or use "
        f"`npx playwright@<version> install --with-deps {browser}` (needs root). Raise "
        f"the startup timeout if the download is cut off.{python}"
    )


# (signature, (runner, setup_failed, command, args, pinned, evidence) -> hint) — first
# match over the log tail wins; ``evidence`` is the lines it matched. Hints see the
# launch shape and current pin state so a recommendation is only ever an action the
# operator can actually take on THIS server — never a toggle that's refused, or one
# that's already on.
_SIGNATURES: list[
    tuple[re.Pattern[str], Callable[[str, bool, str, Sequence[str], bool, Sequence[str]], str]]
] = [
    (_MCP2_IMPORT, _mcp2_hint),
    (_PLAYWRIGHT_BROWSER, _playwright_hint),
]

# Failures repeat per attempt, so the signature is always near the end; a
# bounded tail keeps the scan cheap even against a full 2000-line buffer.
_TAIL_LINES = 400

# The unit's own phase markers (``ServerUnit`` logs one before each phase's
# output: "[mcpelevator] attempt N/M: setup|bridge|readiness|health"; the health
# check's output counts on the launch side, like the bridge's). They section the
# shared log so a signature is only credited to the phase that emitted it — a
# setup script that PRINTS the traceback but succeeds must not put a launch-argv
# remedy on an unrelated later failure, and vice versa.
# The attempt number is bounded (real budgets are single digits): a marker-SHAPED
# line in untrusted child output carrying thousands of digits must fall through as
# ordinary output, not reach ``int()`` — CPython caps int-from-str conversion, so
# an unbounded group would raise and (via the unit's failure path) could block the
# activation from converging to "failed".
_PHASE_MARKER = re.compile(
    r"^\[mcpelevator\] attempt (\d{1,9})/\d+: (checkout|setup|bridge|readiness|health)\b"
)


def startup_hint(
    lines: Iterable[str],
    runner: str,
    *,
    setup_failed: bool = False,
    command: str = "",
    args: Sequence[str] = (),
    pinned: bool = False,
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
            # A checkout section (repository clone + post-clone build) is neither
            # phase: its lines are never evidence for a setup or launch remedy.
            phase = {"setup": "setup", "checkout": "checkout"}.get(marker.group(2), "launch")
            continue
        if phase == wanted:
            relevant.append(line)
    for pattern, hint in _SIGNATURES:
        evidence = [line for line in relevant if pattern.search(line)]
        if evidence:
            return hint(runner, setup_failed, command, args, pinned, evidence)
    return None
