"""Sign-in sessions (ADR-0004): the cookie a browser holds after signing in through the
identity provider, and the CSRF header that must ride with it.

The cookie is ``__Host-mcpe_session`` (HttpOnly, Secure, SameSite=Strict, Path=/), or
``mcpe_session`` without ``Secure`` when the instance's public base is plain http. Its
value is random; only the SHA-256 is stored (``SignInSession.token_hash``). The CSRF
header ``X-MCPE-CSRF`` is an HMAC of the session row's id under ``<data_dir>/session.key``,
handed to the SPA by ``/api/auth/status``. A cross-site page can neither read it nor set it.

A session counts only where ``accepts`` says so: ``/api`` other than ``/api/mcp``, and
never next to an ``Authorization`` header. WHO it names is decided in
``app.auth.principal``; this module only finds the row.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Optional

from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import Response

from app import sealed
from app.api.util import oauth_public_base
from app.config import get_settings
from app.db import repo
from app.db.models import SignInSession
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token, secret_file

SECURE_COOKIE = "__Host-mcpe_session"
PLAIN_COOKIE = "mcpe_session"
CSRF_HEADER = "x-mcpe-csrf"
LANDED_COOKIE = "mcpe_sso_landed"  # frontend/src/lib/auth.ts reads and clears it
# The browser-only headers the proxies and the control MCP's inner calls drop.
BROWSER_HEADERS = frozenset({"cookie", CSRF_HEADER})
# The one /api route that reads the cookie without the CSRF header: it is how the SPA
# learns the header's value. A GET with no side effects, and SameSite=Strict keeps the
# cookie off cross-site requests while CORS keeps the answer from a cross-site reader.
STATUS_PATH = "/api/auth/status"


@lru_cache
def _key() -> bytes:
    return secret_file(
        get_settings().data_dir / "session.key", lambda: secrets.token_bytes(32),
        lambda b: len(b) == 32,
    )


def csrf(session_id: str) -> str:
    return hmac.new(_key(), session_id.encode(), hashlib.sha256).hexdigest()


def https(request: Request) -> bool:
    """Is this instance's public base https? The one rule for which cookie it sets."""
    return oauth_public_base(request).startswith("https://")


def cookie_value(request: Request) -> str:
    """The session cookie for this instance's scheme only. On https a plain
    ``mcpe_session`` is ignored: a sibling host could have planted it with ``Domain=``,
    which ``__Host-`` rules out."""
    return request.cookies.get(SECURE_COOKIE if https(request) else PLAIN_COOKIE, "")


def accepts(request: Request) -> bool:
    """May a Sign-in session authenticate this request at all? Only on ``/api`` (never
    ``/api/mcp``) and only when no ``Authorization`` header is present, so a bearer
    caller is judged on its token alone."""
    path = request.scope.get("path", "")
    if not path.startswith("/api/") or path == "/api/mcp" or path.startswith("/api/mcp/"):
        return False
    return "authorization" not in request.headers


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def hard_expiry(row: SignInSession) -> datetime:
    return _aware(row.expires_at)


def _before_hard_expiry(row: Optional[SignInSession]) -> bool:
    return row is not None and hard_expiry(row) > datetime.now(timezone.utc)


def soft_expired(row: SignInSession) -> bool:
    """Has the provider's access token run out? Then only ``STATUS_PATH`` counts the
    session, and refreshes it."""
    soft = row.soft_expires_at
    return soft is not None and _aware(soft) <= datetime.now(timezone.utc)


def live(row: Optional[SignInSession]) -> bool:
    """Before both its hard and its soft expiry."""
    return _before_hard_expiry(row) and not soft_expired(row)


def cookie_row(request: Request, session: Session) -> Optional[SignInSession]:
    """The session the request's cookie names, soft-expired or not, before its hard
    expiry. For ``STATUS_PATH``, which refreshes a soft-expired one."""
    raw = cookie_value(request)
    row = repo.get_sign_in_session_by_hash(session, hash_token(raw)) if raw else None
    return row if _before_hard_expiry(row) else None


def current(request: Request, session: Session) -> Optional[SignInSession]:
    """The request's live Sign-in session, or None. Requires a matching CSRF header
    everywhere except ``STATUS_PATH``."""
    row = cookie_row(request, session)
    if row is None or soft_expired(row):
        return None
    if request.scope.get("path") != STATUS_PATH:
        presented = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(presented.encode(), csrf(row.id).encode()):
            return None
    return row


def max_age(session: Session) -> timedelta:
    """The hard cap on a new session (the ``oidc_session_max_age_s`` setting)."""
    key = "oidc_session_max_age_s"
    return timedelta(seconds=repo.setting_get(session, key, runtime_settings.DEFAULTS[key]))


@dataclass(frozen=True)
class Grant:
    """What a session keeps from a token response: the ID token, the refresh token
    ("" when the provider issued none) and when the access token runs out."""

    id_token: str
    refresh_token: str
    access_expires_at: datetime

    def columns(self) -> dict:
        """The session row's columns for this grant, sealed. Without a refresh token
        there's nothing to refresh with, so no soft expiry."""
        return {
            "id_token": sealed.seal(self.id_token) if self.id_token else "",
            "refresh_token": sealed.seal(self.refresh_token) if self.refresh_token else "",
            "soft_expires_at": self.access_expires_at if self.refresh_token else None,
        }


def start(
    session: Session, user_id: str, grant: Grant, settings: str,
    still_valid: Callable[[Session], bool] = lambda _s: True,
) -> Optional[str]:
    """Begin a Sign-in session for ``user_id`` from the provider's ``grant``, bound to
    the sign-in settings digest ``settings``; returns the cookie value (shown once).
    ``still_valid`` runs after the row is flushed, so under the database write lock a
    settings write also takes: when it says no, nothing is stored and this returns None."""
    raw = new_token()
    now = datetime.now(timezone.utc)
    session.add(
        SignInSession(
            id=new_id(), token_hash=hash_token(raw), user_id=user_id, created_at=now,
            expires_at=now + max_age(session), settings=settings, **grant.columns(),
        )
    )
    session.flush()
    session.expire_all()  # read what's committed now, not what this session cached
    if not still_valid(session):
        session.rollback()
        return None
    session.commit()
    return raw


def end(request: Request, session: Session) -> Optional[str]:
    """Delete the row the request's cookie names, if any. No CSRF header needed: CSRF
    stops cross-site state changes, and a forced logout harms nobody. Raises if the
    delete fails, so a caller never reports a logout that didn't happen. Returns the
    ended session's ID token ("" when it holds none), or None when there was no session."""
    raw = cookie_value(request)
    row = repo.get_sign_in_session_by_hash(session, hash_token(raw)) if raw else None
    if row is None:
        return None
    id_token = sealed.unseal(row.id_token) or ""
    session.delete(row)
    session.commit()
    return id_token


def set_cookie(response: Response, raw: str, *, https: bool, max_age: timedelta) -> None:
    response.set_cookie(
        SECURE_COOKIE if https else PLAIN_COOKIE, raw,
        max_age=int(max_age.total_seconds()), path="/", secure=https, httponly=True,
        samesite="strict",
    )
    # A readable marker (no secret in it): the SPA that loads next clears any stored
    # login token, which would otherwise be sent instead of the new session.
    response.set_cookie(
        LANDED_COOKIE, "1", max_age=300, path="/", secure=https, samesite="strict"
    )


def clear_cookie(response: Response) -> None:
    for name, secure in ((SECURE_COOKIE, True), (PLAIN_COOKIE, False)):
        response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="strict")
