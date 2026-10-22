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
from functools import lru_cache
from typing import Optional

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
    # ponytail: PyJWKClient fetches synchronously, so a key refresh (hourly, or on an
    # unknown kid) blocks the caller for one short request; move to an async fetch if
    # that ever shows up in latency.
    return jwt.PyJWKClient(f"{team}/cdn-cgi/access/certs", lifespan=3600, timeout=5)


def configured() -> bool:
    s = get_settings()
    return bool(s.cf_access_team_domain.strip() and s.cf_access_aud.strip())


def email(request: Request) -> Optional[str]:
    """The verified email in the request's assertion, lowercased, or None."""
    token = request.headers.get(HEADER, "")
    if not token or not configured():
        return None
    s = get_settings()
    team = _team(s.cf_access_team_domain)
    audience = [a.strip() for a in s.cf_access_aud.split(",") if a.strip()]
    try:
        key = _keys(team).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=audience, issuer=team,
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        logger.info("Cloudflare Access assertion rejected: %s", exc)
        return None
    address = claims.get("email")
    return address.strip().lower() if isinstance(address, str) and address.strip() else None


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
