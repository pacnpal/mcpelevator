"""mcpelevator — elevate stdio MCP servers into authenticated HTTP endpoints."""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path
from typing import Optional


def _version_from_pyproject() -> Optional[str]:
    """Read ``[project].version`` from the sibling ``pyproject.toml``.

    uv treats this project as *virtual* (a self-hosted app, not a distributed package), so
    there's no installed metadata to read for a source checkout — parse the file directly so a
    local ``uv run`` still reports the right version instead of an ``unknown`` placeholder.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        # TypeError guards a structurally-malformed file (e.g. ``project`` parsed as a
        # non-table), so a bad pyproject can never crash the import of ``app``.
        return None


def _resolve_version() -> str:
    """Resolve the running version, deriving from the GitHub release tag wherever possible.

    Priority:
    1. ``MCPE_VERSION`` — injected into the published image from the release tag (the CI
       release workflow passes the tag as ``APP_VERSION``; the ``Dockerfile`` sets it as
       ``MCPE_VERSION``). This is what makes a deployed container report e.g. ``1.2.3``.
    2. Installed package metadata (``pip``/``uv`` install of a packaged build).
    3. ``pyproject.toml`` — for a source checkout run in place (the common dev case).
    4. A ``0.0.0+unknown`` fallback.

    There is intentionally NO hardcoded version literal here — the release tag is the single
    source of truth, and a stale constant would only drift from it.
    """
    env = os.environ.get("MCPE_VERSION")
    if env:
        return env
    try:
        return metadata.version("mcpelevator")
    except metadata.PackageNotFoundError:
        pass
    return _version_from_pyproject() or "0.0.0+unknown"


__version__ = _resolve_version()
