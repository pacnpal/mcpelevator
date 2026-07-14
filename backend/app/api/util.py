"""Shared API helpers."""

from __future__ import annotations

import logging

from starlette.requests import Request

from app.config import get_settings

logger = logging.getLogger(__name__)


async def resync_groups(request: Request) -> None:
    """Converge the group hub NOW instead of on the next reconcile, so a membership- or
    auth-affecting change (a server delete/edit, a group write, a default-auth change)
    takes effect before the handler returns rather than leaving a stale mounted set
    serveable in the gap. Shared fail-safe used by the server, group, and settings
    routers: ``sync()`` isolates per-group failures internally (a bad group fails closed
    to 503 without blocking the rest), so a raised exception here is a broader failure —
    log the traceback and let the reconciler re-converge on its next pass; never fail the
    already-committed write. ``sync()`` is lock-serialized and task-safe."""
    try:
        await request.app.state.groups.sync(request.app.state.supervisor)
    except Exception:  # the registry write already committed; don't fail the call
        logger.exception("group resync failed")


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
