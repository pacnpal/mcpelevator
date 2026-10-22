"""Cloudflare Access identity for the built-in mcpelevator server.

Behind Cloudflare Access, the edge signs every request it lets through with a
``Cf-Access-Jwt-Assertion`` JWT naming the person who signed in. OAuth-only MCP
clients (claude.ai connectors, MCPJam) sign in at the edge and never hold an
mcpelevator token, so this is how the control plane learns who they are.

Opt-in: both ``MCPE_CF_ACCESS_TEAM_DOMAIN`` and ``MCPE_CF_ACCESS_AUD`` must be set.
The assertion is checked against the team's signing keys, issuer and AUD tag, and
its email is matched (case-insensitively) to a user's name. No match, no identity.

Trust stays narrow. The edge attaches the assertion to every request, browser ones
included, so accepting it on all of ``/api`` would make the SPA's routes forgeable
cross-site. ``principal.resolve`` only honors it on requests the built-in bridge
carried (``app.runners.builtin.is_internal``); the ``/s`` and ``/g`` auth checks for
the built-in server use it directly.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Optional

import jwt
from sqlmodel import Session
from starlette.requests import Request

from app.config import get_settings
from app.db import repo
from app.db.models import User

logger = logging.getLogger(__name__)

HEADER = "cf-access-jwt-assertion"


def _team(domain: str) -> str:
    domain = domain.strip().rstrip("/")
    return domain if domain.startswith("https://") else f"https://{domain}"


@lru_cache(maxsize=4)
def _keys(team: str) -> jwt.PyJWKClient:
    # ponytail: PyJWKClient fetches synchronously, so a key refresh (hourly, or at most
    # every _REFETCH_S on an unknown kid) blocks the caller for one short request; move
    # to an async fetch if that ever shows up in latency.
    return jwt.PyJWKClient(f"{team}/cdn-cgi/access/certs", lifespan=3600, timeout=5)


# An unknown kid refetches the keys (Cloudflare rotates them), but no more often than
# this: otherwise every junk assertion would cost a blocking outbound request.
_REFETCH_S = 300.0
_last_refetch: dict[str, float] = {}
# After a failed fetch (the certs endpoint down or slow), refuse for this long
# instead of letting every queued assertion block on another doomed fetch.
_FAILED_BACKOFF_S = 30.0
_last_failure: dict[str, float] = {}


def _fetch(team: str, *, refresh: bool) -> dict[str, Any]:
    client = _keys(team)
    cached = client.jwk_set_cache.get() if client.jwk_set_cache is not None and not refresh else None
    if cached is not None:
        # Still-fresh keys never wait on the backoff: a failed refetch for one junk
        # kid must not lock out every valid sign-in.
        return {k.key_id: k.key for k in cached.keys}
    if time.monotonic() - _last_failure.get(team, -_FAILED_BACKOFF_S) < _FAILED_BACKOFF_S:
        raise jwt.PyJWKClientConnectionError("Cloudflare's signing keys were unreachable moments ago")
    try:
        return {k.key_id: k.key for k in client.get_signing_keys(refresh=refresh)}
    except jwt.PyJWKClientConnectionError:
        _last_failure[team] = time.monotonic()
        raise


def _signing_key(team: str, token: str) -> Any:
    kid = jwt.get_unverified_header(token).get("kid")
    keys = _fetch(team, refresh=False)
    if kid not in keys and time.monotonic() - _last_refetch.get(team, -_REFETCH_S) >= _REFETCH_S:
        keys = _fetch(team, refresh=True)
        # Only a refetch that worked starts the window: one that failed is covered by
        # the shorter failure backoff, so a key rotated during an outage is picked up
        # as soon as Cloudflare answers again.
        _last_refetch[team] = time.monotonic()
    if kid not in keys:
        raise jwt.InvalidTokenError("signed with a key the team doesn't publish")
    return keys[kid]


def configured() -> bool:
    s = get_settings()
    return bool(s.cf_access_team_domain.strip() and s.cf_access_aud.strip())


def verify(request: Request) -> tuple[Optional[str], Optional[str]]:
    """``(email, None)`` for a verified assertion, else ``(None, why)``. ``why`` names
    what an operator can fix (expired, wrong AUD tag, wrong team domain) and stays
    generic for anything else, so a forged token learns nothing from it."""
    token = request.headers.get(HEADER, "")
    if not token:
        return None, "no Cloudflare Access assertion"
    if not configured():
        return None, ("a Cloudflare Access assertion arrived, but MCPE_CF_ACCESS_TEAM_DOMAIN "
                      "and MCPE_CF_ACCESS_AUD aren't set")
    s = get_settings()
    team = _team(s.cf_access_team_domain)
    audience = [a.strip() for a in s.cf_access_aud.split(",") if a.strip()]
    try:
        claims = jwt.decode(
            token, _signing_key(team, token), algorithms=["RS256"], audience=audience,
            issuer=team, options={"require": ["exp", "iat"]},
        )
    except jwt.ExpiredSignatureError:
        return None, "the Cloudflare Access assertion has expired; sign in again"
    except jwt.InvalidAudienceError:
        return None, ("the Cloudflare Access assertion is for a different Access application "
                      "(check MCPE_CF_ACCESS_AUD)")
    except jwt.InvalidIssuerError:
        return None, ("the Cloudflare Access assertion is from a different team domain "
                      "(check MCPE_CF_ACCESS_TEAM_DOMAIN)")
    except jwt.PyJWTError as exc:
        logger.info("Cloudflare Access assertion rejected: %s", exc)
        return None, "the Cloudflare Access assertion didn't verify"
    address = claims.get("email")
    if not isinstance(address, str) or not address.strip():
        return None, "the Cloudflare Access assertion carries no email"
    return address.strip().lower(), None


def email(request: Request) -> Optional[str]:
    """The verified email in the request's assertion, lowercased, or None."""
    return verify(request)[0]


def user(session: Session, request: Request) -> Optional[User]:
    """The user whose name is the request's verified Cloudflare Access email. User
    names aren't unique, so two matches is no match: guessing could hand a member an
    admin's identity."""
    address = email(request)
    if address is None:
        return None
    matches = [u for u in repo.list_users(session) if u.name.strip().lower() == address]
    if len(matches) != 1:
        logger.info("Cloudflare Access signed in %s, but %d users have that name",
                    address, len(matches))
        return None
    return matches[0]
