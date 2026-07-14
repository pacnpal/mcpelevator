"""The ``oauth`` auth provider — OAuth 2.1 resource server backed by an EXTERNAL
authorization server (RFC 9728).

mcpelevator deliberately does not implement an authorization server. Instead, this
provider validates JWT access tokens minted by one the operator already runs
(Authentik, Keycloak, Auth0, ...), and advertises it via Protected Resource
Metadata so OAuth-only MCP clients (claude.ai web/mobile custom connectors) can
discover it and drive the flow themselves:

    client -> 401 + WWW-Authenticate: resource_metadata=...
           -> GET /.well-known/oauth-protected-resource/s/<slug>/mcp
           -> authorization_servers: [<issuer>]  -> AS metadata / DCR / PKCE at the AS
           -> Bearer <jwt> -> verified here against the AS's JWKS

Configuration is three runtime settings (see ``app.registry.settings``):

* ``oauth_config_url`` — the AS's OIDC discovery / RFC 8414 metadata URL. A bare
  issuer URL also works (``/.well-known/openid-configuration`` is appended).
* ``oauth_audience`` — optional ``aud`` to require in tokens.
* ``oauth_allowed_subjects`` — optional identity allowlist, matched (case-
  insensitively) against ``preferred_username`` / ``login`` / ``email`` / ``sub``.

Verification is delegated to fastmcp's ``JWTVerifier`` (already a core dependency —
the bridges are fastmcp proxies), so JWKS fetch/cache/rotation and JWT validation
are not reimplemented here. Discovery documents are cached for an hour; changing
``oauth_config_url``/``oauth_audience`` takes effect immediately because the
verifier cache is keyed by both values.

NOTE for clients without Dynamic Client Registration support at the AS (e.g.
Authentik as of 2026): claude.ai custom connectors accept a manually-configured
OAuth client id/secret, which skips DCR — see docs/claude-web-exposure.md.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from sqlmodel import Session
from starlette.requests import Request

from app.api.util import base_url
from app.db import get_engine, repo
from app.db.models import Server
from app.registry import settings as runtime_settings

_DISCOVERY_TTL_S = 3600.0

# discovery-url -> (fetched_at, document). Small and process-local; a config change
# uses a new key, and staleness is bounded by the TTL (JWKS rotation is handled
# inside JWTVerifier, which re-fetches keys it doesn't recognize).
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# (config_url, audience) -> verifier. Rebuilt whenever either setting changes.
_verifier_cache: dict[tuple[str, str], Any] = {}


def _normalize_config_url(config_url: str) -> str:
    """Accept either a full metadata URL or a bare issuer; issuers get the OIDC
    well-known path appended (the common case for operators pasting an issuer)."""
    url = config_url.rstrip("/")
    if ".well-known" in url:
        return url
    return f"{url}/.well-known/openid-configuration"


async def _discovery(config_url: str) -> dict[str, Any]:
    url = _normalize_config_url(config_url)
    cached = _discovery_cache.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < _DISCOVERY_TTL_S:
        return cached[1]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    if not doc.get("issuer") or not doc.get("jwks_uri"):
        raise ValueError(f"AS metadata at {url} lacks issuer/jwks_uri")
    _discovery_cache[url] = (now, doc)
    return doc


async def _verifier_for(config_url: str, audience: str):
    """Build (or reuse) a JWTVerifier bound to the AS's issuer + JWKS."""
    key = (config_url, audience)
    verifier = _verifier_cache.get(key)
    if verifier is None:
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        doc = await _discovery(config_url)
        verifier = JWTVerifier(
            jwks_uri=doc["jwks_uri"],
            issuer=doc["issuer"],
            audience=audience or None,
        )
        _verifier_cache[key] = verifier
    return verifier


def _metadata_url(base: str, slug: str) -> str:
    # RFC 9728 §3: well-known inserted between host and the resource path.
    return f"{base}/.well-known/oauth-protected-resource/s/{slug}/mcp"


def _challenge(base: str, slug: str, error: Optional[str] = None) -> dict[str, str]:
    parts = ["Bearer"]
    if error:
        parts[0] += f' error="{error}",'
    parts.append(f'resource_metadata="{_metadata_url(base, slug)}"')
    return {"WWW-Authenticate": " ".join(parts)}


def _identity(claims: dict[str, Any]) -> list[str]:
    """Candidate identity strings for the allowlist check, lowercased."""
    return [
        str(claims[k]).lower()
        for k in ("preferred_username", "login", "email", "sub")
        if claims.get(k)
    ]


class OAuthProvider:
    name = "oauth"

    async def authenticate(self, request: Request, server: Server) -> None:
        with Session(get_engine()) as session:
            config_url = runtime_settings.oauth_config_url(session)
            audience = runtime_settings.oauth_audience(session)
            allowed = runtime_settings.oauth_allowed_subjects(session)
            accept_bearer = runtime_settings.oauth_accept_bearer(session)
        # Fail closed, like resolve() does for an unknown provider: a server marked
        # oauth with no AS configured must not fall through to unauthenticated.
        if not config_url:
            raise HTTPException(status_code=403, detail="oauth provider not configured")

        base = base_url(request)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()

        # Opt-in bearer coexistence (oauth_accept_bearer): one endpoint serving OAuth
        # humans AND token-carrying automation. Local tokens are unambiguous — every
        # minted token is "mcpe_"-prefixed (see util.new_token) and no JWT can start
        # that way (base64url of any JSON header can't) — so delegation is exact: a
        # local-looking token gets the bearer provider's verdict (including its 403
        # scope semantics) and never falls through to JWT parsing, and vice versa.
        if accept_bearer and token.startswith("mcpe_"):
            from app.auth.bearer import BearerProvider

            return await BearerProvider().authenticate(request, server)
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=401,
                detail="missing bearer token",
                headers=_challenge(base, server.slug),
            )
        try:
            verifier = await _verifier_for(config_url, audience)
            access = await verifier.verify_token(token)
        except HTTPException:
            raise
        except Exception as exc:
            # AS metadata/JWKS unreachable is OUR outage, not the client's: a 401
            # would send well-behaved clients into a pointless re-auth loop.
            raise HTTPException(
                status_code=503, detail=f"authorization server unreachable: {exc}"
            ) from exc
        if access is None:
            raise HTTPException(
                status_code=401,
                detail="invalid token",
                headers=_challenge(base, server.slug, error="invalid_token"),
            )
        if allowed:
            allowed_lower = {a.lower() for a in allowed}
            claims = getattr(access, "claims", None) or {}
            if not allowed_lower & set(_identity(claims)):
                raise HTTPException(
                    status_code=403,
                    detail="token subject not authorized for this server",
                    headers=_challenge(base, server.slug, error="insufficient_scope"),
                )


# --- Protected Resource Metadata (RFC 9728) -------------------------------------
# Public by design (clients fetch it pre-auth), so the router is mounted ungated.
# Only servers whose EFFECTIVE provider is ``oauth`` are advertised; everything
# else 404s so the endpoint doesn't leak which slugs exist.

wellknown = APIRouter()


def _effective_oauth(slug: str) -> bool:
    from app.aggregate.hub import AGGREGATE_SLUG  # local: avoid import cycle

    with Session(get_engine()) as session:
        default = runtime_settings.default_auth_provider(session)
        config_url = runtime_settings.oauth_config_url(session)
        if not config_url:
            return False
        if slug == AGGREGATE_SLUG:
            return runtime_settings.unified_endpoint(session) and default == "oauth"
        server = repo.get_server_by_slug(session, slug)
        if server is None:
            return False
        name = server.auth_provider if server.auth_provider != "inherit" else default
        return name == "oauth"


@wellknown.get("/.well-known/oauth-protected-resource/s/{slug}/mcp")
async def protected_resource_metadata(slug: str, request: Request):
    if not _effective_oauth(slug):
        raise HTTPException(status_code=404, detail="not found")
    with Session(get_engine()) as session:
        config_url = runtime_settings.oauth_config_url(session)
    try:
        doc = await _discovery(config_url)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="authorization server metadata unavailable"
        ) from exc
    base = base_url(request)
    return {
        "resource": f"{base}/s/{slug}/mcp",
        "authorization_servers": [doc["issuer"]],
        "bearer_methods_supported": ["header"],
    }
