"""Shared API helpers."""

from __future__ import annotations

from starlette.requests import Request

from app.config import get_settings


def base_url(request: Request) -> str:
    """Base URL for copyable links (server copy menu, group endpoints). Prefer the
    operator-declared public URL; otherwise use the host the client actually reached
    us on — so a LAN device (with ``allow_private_lan``) copies
    ``http://192.168.1.50:8080/...`` rather than the ``0.0.0.0``→``127.0.0.1`` rewrite
    baked into ``settings.base_url``. The Host header is already validated by the
    control-plane allowlist before any handler runs, so it's a trusted value here.
    Falls back to the derived settings URL when there's no Host."""
    settings = get_settings()
    if settings.public_base_url:
        return settings.base_url  # operator-declared canonical URL wins
    host = request.headers.get("host", "").strip()
    if host:
        return f"{request.url.scheme or 'http'}://{host}"
    return settings.base_url
