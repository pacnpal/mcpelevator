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
from typing import Optional

import jwt
from sqlmodel import Session
from starlette.requests import Request

from app.auth import jwks
from app.config import get_settings
from app.db import repo
from app.db.models import User

logger = logging.getLogger(__name__)

HEADER = "cf-access-jwt-assertion"


def _team(domain: str) -> str:
    domain = domain.strip().rstrip("/")
    return domain if domain.startswith("https://") else f"https://{domain}"


def _certs(team: str) -> str:
    return f"{team}/cdn-cgi/access/certs"


def prewarm() -> None:
    """Load the keys in the background at startup, so the first sign-in has them."""
    if configured():
        jwks.prewarm(_certs(_team(get_settings().cf_access_team_domain)))


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
            token, jwks.signing_key(_certs(team), token), algorithms=["RS256"], audience=audience,
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
    except jwt.PyJWKClientConnectionError as exc:
        logger.info("Cloudflare Access keys unavailable: %s", exc)
        return None, "Cloudflare Access's signing keys aren't available yet; try again shortly"
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
