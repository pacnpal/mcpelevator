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
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import Response

from app import sealed
from app.config import get_settings
from app.db import repo
from app.db.models import SignInSession
from app.util import hash_token, new_id, new_token, secret_file

SECURE_COOKIE = "__Host-mcpe_session"
PLAIN_COOKIE = "mcpe_session"
CSRF_HEADER = "x-mcpe-csrf"
# The browser-only headers the proxies and the control MCP's inner calls drop.
BROWSER_HEADERS = frozenset({"cookie", CSRF_HEADER})
# Hard expiry, fixed for now; #273 adds a soft expiry and refresh.
MAX_AGE = timedelta(hours=12)
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


def cookie_value(request: Request) -> str:
    return request.cookies.get(SECURE_COOKIE) or request.cookies.get(PLAIN_COOKIE) or ""


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


def live(row: Optional[SignInSession]) -> bool:
    return row is not None and _aware(row.expires_at) > datetime.now(timezone.utc)


def current(request: Request, session: Session) -> Optional[SignInSession]:
    """The request's live Sign-in session, or None. Requires a matching CSRF header
    everywhere except ``STATUS_PATH``."""
    raw = cookie_value(request)
    if not raw:
        return None
    row = repo.get_sign_in_session_by_hash(session, hash_token(raw))
    if not live(row):
        return None
    if request.scope.get("path") != STATUS_PATH:
        presented = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(presented.encode(), csrf(row.id).encode()):
            return None
    return row


def start(session: Session, user_id: str, id_token: str) -> str:
    """Begin a Sign-in session for ``user_id``; returns the cookie value (shown once)."""
    raw = new_token()
    now = datetime.now(timezone.utc)
    session.add(
        SignInSession(
            id=new_id(), token_hash=hash_token(raw), user_id=user_id, created_at=now,
            expires_at=now + MAX_AGE, id_token=sealed.seal(id_token) if id_token else "",
        )
    )
    session.commit()
    return raw


def end(request: Request, session: Session) -> None:
    """Delete the row the request's cookie names, if any."""
    raw = cookie_value(request)
    row = repo.get_sign_in_session_by_hash(session, hash_token(raw)) if raw else None
    if row is not None:
        session.delete(row)
        session.commit()


def set_cookie(response: Response, raw: str, *, https: bool) -> None:
    response.set_cookie(
        SECURE_COOKIE if https else PLAIN_COOKIE, raw,
        max_age=int(MAX_AGE.total_seconds()), path="/", secure=https, httponly=True,
        samesite="strict",
    )


def clear_cookie(response: Response) -> None:
    for name, secure in ((SECURE_COOKIE, True), (PLAIN_COOKIE, False)):
        response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="strict")
