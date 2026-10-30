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
    payload = {
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
    # Omitted (not merely `False`) when off, unlike every other field above: those are either
    # required or already always present at whatever schema version wrote them, but this key
    # is NEW. Always including it — even as `False` — would grow every already-persisted spec
    # by a few bytes on the next reconcile; a spec that was accepted right up against
    # BRIDGE_SPEC_MAX_BYTES before this field existed could cross it on upgrade alone, and the
    # startup hash backfill (unlike a live write) never re-runs `_require_launchable_spec` — so
    # a previously-valid, already-enabled server would reach `create_subprocess_exec` and die
    # with `E2BIG` instead of failing the write that grew it. `_tool_transform` already reads a
    # missing key as falsy (`spec.get("normalize_schema_dialect")`), so omitting it when unset
    # is a no-op for the bridge, not a behavior change.
    if spec.normalize_schema_dialect:
        payload["normalize_schema_dialect"] = True
    # Same rule, same reason: a new key, so present only when on. The bridge reads a missing
    # key as off (`spec.get("serialize_client_requests")` in `_build_transport`).
    if spec.serialize_client_requests:
        payload["serialize_client_requests"] = True
    if spec.forward_authorization:
        payload["forward_authorization"] = True
    if spec.instructions:
        payload["instructions"] = spec.instructions
    # Same rule: present only for a server launched from a source checkout, whose child
    # must not inherit the control plane's own virtualenv (`_child_env`).
    if spec.repo:
        payload["checkout"] = True
    # Same rule: the stored OpenAPI document itself follows on the bridge's stdin (it can
    # run to megabytes, far past this env var's limit), so only the flag rides here.
    if spec.openapi_document:
        payload["document_on_stdin"] = True
    return payload


def serialize(payload: dict) -> str:
    return json.dumps(payload)


def oversize(serialized: str) -> int | None:
    """The payload's byte size when it exceeds the limit, else ``None``."""
    size = len(serialized.encode())
    return size if size > BRIDGE_SPEC_MAX_BYTES else None
