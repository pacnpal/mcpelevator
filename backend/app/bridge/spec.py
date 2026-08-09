"""The literal launch spec handed to a bridge process, and the size rule that governs it.

Shared so the WRITE path and the START path agree by construction. The spec travels to
the bridge in one environment variable, and Linux caps a single exec string at 128 KiB
(``MAX_ARG_STRLEN``) — the whole entry, so ``MCPE_BRIDGE_SPEC=`` and the terminating NUL
come out of the same budget. Past it, ``create_subprocess_exec`` raises ``E2BIG``.

Checking only at start time isn't enough: the reconciler STOPS a healthy bridge before
starting the replacement, so a config that's accepted and then can't be exec'd takes the
endpoint offline and leaves the oversized row persisted until someone edits it back. The
registry runs the same check before committing, which turns that outage into a 400.
"""

from __future__ import annotations

import json

BRIDGE_SPEC_ENV_KEY = "MCPE_BRIDGE_SPEC"
BRIDGE_SPEC_MAX_BYTES = 128 * 1024 - len(BRIDGE_SPEC_ENV_KEY) - 2


def bridge_payload(spec, name: str, *, mcp_http: bool, rest_openapi: bool) -> dict:
    """The JSON the bridge process reads out of its environment."""
    return {
        "command": spec.command,
        "args": spec.args,
        "env": spec.env,
        "cwd": spec.cwd,
        "transport": spec.transport,
        "minimal_env": spec.minimal_env,
        "oauth": spec.oauth,
        "disabled_tools": list(spec.disabled_tools or []),
        "tool_overrides": {k: dict(v) for k, v in (spec.tool_overrides or {}).items()},
        "name": name,
        "mcp_http": mcp_http,
        "rest_openapi": rest_openapi,
    }


def serialize(payload: dict) -> str:
    return json.dumps(payload)


def oversize(serialized: str) -> int | None:
    """The payload's byte size when it exceeds the limit, else ``None``."""
    size = len(serialized.encode())
    return size if size > BRIDGE_SPEC_MAX_BYTES else None
