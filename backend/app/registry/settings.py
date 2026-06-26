"""Runtime-mutable settings, stored in the SQLite ``setting`` kv table.

Distinct from ``app.config`` (process/env bootstrap settings): these are the
security knobs the UI edits at runtime — bind mode, the Host/Origin allowlist,
and the default auth provider for servers set to ``inherit``.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.db import repo
from app.util import host_only

DEFAULTS: dict[str, Any] = {
    "bind_mode": "local",  # 'local' | 'expose'
    "allowed_hosts": [],  # Host/Origin allowlist when exposed (DNS-rebinding defense)
    "default_auth_provider": "none",  # 'none' | 'bearer' — used when a server is 'inherit'
}


def read_all(session: Session) -> dict[str, Any]:
    return {key: repo.setting_get(session, key, default) for key, default in DEFAULTS.items()}


_MODES = {"local", "expose"}
_PROVIDERS = {"none", "bearer"}


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
        out.append(host)
    return out


def write(session: Session, changes: dict[str, Any]) -> dict[str, Any]:
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
        if key == "allowed_hosts":
            value = _normalize_hosts(value)
        pending[key] = value
    repo.setting_set_many(session, pending)
    return read_all(session)


def bind_mode(session: Session) -> str:
    return repo.setting_get(session, "bind_mode", DEFAULTS["bind_mode"])


def allowed_hosts(session: Session) -> list[str]:
    return repo.setting_get(session, "allowed_hosts", DEFAULTS["allowed_hosts"])


def default_auth_provider(session: Session) -> str:
    return repo.setting_get(session, "default_auth_provider", DEFAULTS["default_auth_provider"])
