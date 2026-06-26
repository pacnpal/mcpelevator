"""Runtime-mutable settings, stored in the SQLite ``setting`` kv table.

Distinct from ``app.config`` (process/env bootstrap settings): these are the
security knobs the UI edits at runtime — bind mode, the Host/Origin allowlist,
and the default auth provider for servers set to ``inherit``.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.db import repo

DEFAULTS: dict[str, Any] = {
    "bind_mode": "local",  # 'local' | 'expose'
    "allowed_hosts": [],  # Host/Origin allowlist when exposed (DNS-rebinding defense)
    "default_auth_provider": "none",  # 'none' | 'bearer' — used when a server is 'inherit'
}


def read_all(session: Session) -> dict[str, Any]:
    return {key: repo.setting_get(session, key, default) for key, default in DEFAULTS.items()}


def write(session: Session, changes: dict[str, Any]) -> dict[str, Any]:
    for key, value in changes.items():
        if key in DEFAULTS:
            repo.setting_set(session, key, value)
    return read_all(session)


def bind_mode(session: Session) -> str:
    return repo.setting_get(session, "bind_mode", DEFAULTS["bind_mode"])


def allowed_hosts(session: Session) -> list[str]:
    return repo.setting_get(session, "allowed_hosts", DEFAULTS["allowed_hosts"])


def default_auth_provider(session: Session) -> str:
    return repo.setting_get(session, "default_auth_provider", DEFAULTS["default_auth_provider"])
