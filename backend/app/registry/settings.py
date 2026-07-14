"""Runtime-mutable settings, stored in the SQLite ``setting`` kv table.

Distinct from ``app.config`` (process/env bootstrap settings): these are the
security knobs the UI edits at runtime — bind mode, the Host/Origin allowlist,
and the default auth provider for servers set to ``inherit``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlmodel import Session

from app.db import repo
from app.util import host_only

DEFAULTS: dict[str, Any] = {
    "bind_mode": "local",  # 'local' | 'expose'
    "allowed_hosts": [],  # Host/Origin allowlist when exposed (DNS-rebinding defense)
    "default_auth_provider": "none",  # 'none' | 'bearer' — used when a server is 'inherit'
    "control_plane_auth": "auto",  # 'auto' (require iff expose) | 'always' — gates /api bearer auth
    # Allow private-IP-literal Hosts (e.g. http://192.168.1.50:8080) from a peer on a
    # private network, so a self-hosted box (Unraid, NAS) is reachable from other LAN
    # devices without per-host allowlisting. Rebinding-safe (a rebound attack sends a
    # domain, not a private-IP literal). Counts as "reachable off-host" so 'auto'
    # control-plane auth turns on when it's enabled — see app.auth.control_plane.
    "allow_private_lan": False,
    # Enable the docker runner (launch MCP servers packaged as Docker/OCI images). OFF by
    # default and ROOT-EQUIVALENT: it runs arbitrary images on the mounted Docker daemon
    # (sibling containers on the host, or an isolated dind sidecar via DOCKER_HOST). The
    # service/supervisor refuse to enable or start a docker server while this is off.
    "docker_runner": False,
    # Serve the unified MCP endpoint at /s/all/mcp — one aggregated surface bundling
    # the tools of the running servers, namespaced by slug. OFF by default: it's one
    # URL that reaches many servers' tools, so exposed setups must opt in deliberately.
    "unified_endpoint": False,
    # Which servers the unified endpoint bundles: "all" (every running server) or a
    # list of server ids (the operator-picked subset). Ids of since-deleted servers
    # are simply ignored at mount time.
    "unified_servers": "all",
}


def read_all(session: Session) -> dict[str, Any]:
    return {key: repo.setting_get(session, key, default) for key, default in DEFAULTS.items()}


_MODES = {"local", "expose"}
_PROVIDERS = {"none", "bearer"}
_CONTROL_PLANE_AUTH_MODES = {"auto", "always"}


def _normalize_unified_servers(value: Any) -> Any:
    """Validate the unified-endpoint membership: the literal ``"all"`` or a list of
    server-id strings (deduped, order kept). Structural only — ids of deleted servers
    are ignored at mount time rather than rejected here, so a delete can never strand
    an unwritable setting."""
    if value == "all":
        return "all"
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"invalid unified_servers: {value!r}")
    out: list[str] = []
    for v in value:
        if v not in out:
            out.append(v)
    return out


def _normalize_hosts(hosts: Any) -> list[str]:
    """Validate + normalize allowlist entries to bare hostnames, rejecting empties
    and malformed values (e.g. an unmatched IPv6 bracket) so a bad entry can never
    be stored and later crash the allowlist check. A pasted URL/host:port is
    reduced to its hostname."""
    if not isinstance(hosts, list):
        raise ValueError("allowed_hosts must be a list")
    out: list[str] = []
    for h in hosts:
        host = host_only(h) if isinstance(h, str) else ""
        if not host:
            raise ValueError(f"invalid allowed host: {h!r}")
        if host not in out:  # dedupe post-normalization (e.g. host vs host:port); keep order
            out.append(host)
    return out


def write(
    session: Session, changes: dict[str, Any], *, guard: Callable[[Session], None] | None = None
) -> dict[str, Any]:
    """Persist setting changes. Validates + normalizes the WHOLE patch first, then
    commits it in one step (the SSOT write path), so a bad value can never reach
    storage and a multi-field patch never *partially* applies — a partial write
    could e.g. flip bind_mode and lock out the control plane while still erroring."""
    pending: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key == "bind_mode" and value not in _MODES:
            raise ValueError(f"invalid bind_mode: {value!r}")
        if key == "default_auth_provider" and value not in _PROVIDERS:
            raise ValueError(f"invalid default_auth_provider: {value!r}")
        if key == "control_plane_auth" and value not in _CONTROL_PLANE_AUTH_MODES:
            raise ValueError(f"invalid control_plane_auth: {value!r}")
        if key == "allow_private_lan" and not isinstance(value, bool):
            raise ValueError(f"invalid allow_private_lan: {value!r}")
        if key == "docker_runner" and not isinstance(value, bool):
            raise ValueError(f"invalid docker_runner: {value!r}")
        if key == "unified_endpoint" and not isinstance(value, bool):
            raise ValueError(f"invalid unified_endpoint: {value!r}")
        if key == "unified_servers":
            value = _normalize_unified_servers(value)
        if key == "allowed_hosts":
            value = _normalize_hosts(value)
        pending[key] = value
    repo.setting_set_many(session, pending, guard=guard)
    return read_all(session)


def bind_mode(session: Session) -> str:
    return repo.setting_get(session, "bind_mode", DEFAULTS["bind_mode"])


def allowed_hosts(session: Session) -> list[str]:
    return repo.setting_get(session, "allowed_hosts", DEFAULTS["allowed_hosts"])


def default_auth_provider(session: Session) -> str:
    return repo.setting_get(session, "default_auth_provider", DEFAULTS["default_auth_provider"])


def control_plane_auth(session: Session) -> str:
    return repo.setting_get(session, "control_plane_auth", DEFAULTS["control_plane_auth"])


def allow_private_lan(session: Session) -> bool:
    return repo.setting_get(session, "allow_private_lan", DEFAULTS["allow_private_lan"])


def docker_runner(session: Session) -> bool:
    return repo.setting_get(session, "docker_runner", DEFAULTS["docker_runner"])


def unified_endpoint(session: Session) -> bool:
    return repo.setting_get(session, "unified_endpoint", DEFAULTS["unified_endpoint"])


def unified_servers(session: Session) -> Any:
    """``"all"`` or a list of server ids (see DEFAULTS)."""
    return repo.setting_get(session, "unified_servers", DEFAULTS["unified_servers"])
