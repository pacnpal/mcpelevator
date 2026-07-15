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

Core configuration is stored in runtime settings (see ``app.registry.settings``):

* ``oauth_config_url`` — the AS's OIDC discovery / RFC 8414 metadata URL. A bare
  issuer URL also works (``/.well-known/openid-configuration`` is appended).
* ``oauth_audience`` — required ``aud`` to require in tokens.
* ``oauth_allowed_subjects`` — optional identity allowlist. Friendly username,
  login, and email claims match case-insensitively; OIDC ``sub`` matches exactly.

Signature, issuer, and audience checks are delegated to fastmcp's ``JWTVerifier``
(already a core dependency — the bridges are fastmcp proxies). This module wraps
its JWKS path to classify authorization-server outages, bound unknown-key refreshes,
and require current, expiring tokens. Discovery documents are cached for an hour;
changing ``oauth_config_url``/``oauth_audience`` takes effect immediately because
the verifier cache is keyed by both values.

NOTE for clients without Dynamic Client Registration support at the AS (e.g.
Authentik as of 2026): claude.ai custom connectors accept a manually-configured
OAuth client id/secret, which skips DCR — see docs/claude-web-exposure.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from fastmcp.server.auth.providers.jwt import JWTVerifier, _jwk_to_pem
from sqlmodel import Session
from starlette.requests import Request

from app.api.util import base_url
from app.config import get_settings
from app.db import get_engine, repo
from app.db.models import Server
from app.registry import settings as runtime_settings

logger = logging.getLogger(__name__)

_DISCOVERY_TTL_S = 3600.0
_JWKS_MISS_REFRESH_COOLDOWN_S = 30.0
_CLOCK_SKEW_S = 60.0

# discovery-url -> (fetched_at, document). Small and process-local; a config change
# uses a new key, and staleness is bounded by the TTL (JWKS rotation is handled
# inside JWTVerifier, which re-fetches keys it doesn't recognize).
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# (config_url, audience, jwks_uri, issuer, algorithm) -> verifier. The algorithm
# comes from a strict asymmetric allowlist, so providers using ES*/PS*/RS* work
# without accepting symmetric or unsigned tokens.
_verifier_cache: dict[tuple[str, str, str, str, str], Any] = {}

_ALLOWED_JWT_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "ES256",
        "ES384",
        "ES512",
        "PS256",
        "PS384",
        "PS512",
    }
)


class _AuthorizationServerUnavailable(Exception):
    pass


class _OAuthJWTVerifier(JWTVerifier):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._jwks_refresh_lock = asyncio.Lock()
        self._last_miss_refresh = 0.0
        self._last_miss_unavailable = False

    async def _get_jwks_key(self, kid: str | None) -> str:
        """Collapse unknown-key refreshes so random kids cannot hammer the AS."""
        async with self._jwks_refresh_lock:
            cache_hit = (kid is not None and kid in self._jwks_cache) or (
                kid is None and len(self._jwks_cache) == 1
            )
            now = time.monotonic()
            if (
                not cache_hit
                and now - self._last_miss_refresh < _JWKS_MISS_REFRESH_COOLDOWN_S
            ):
                if self._last_miss_unavailable:
                    raise _AuthorizationServerUnavailable("JWKS refresh unavailable")
                raise ValueError("key ID not found in cached JWKS")
            try:
                return await super()._get_jwks_key(kid)
            except _AuthorizationServerUnavailable:
                self._last_miss_refresh = time.monotonic()
                self._last_miss_unavailable = True
                raise
            except ValueError:
                # FastMCP populates its cache before reporting a genuinely unknown
                # kid. Cool down further misses while still allowing key rotation.
                if time.time() - self._jwks_cache_time < self._cache_ttl:
                    self._last_miss_refresh = time.monotonic()
                    self._last_miss_unavailable = False
                raise

    async def _fetch_jwks(self) -> dict[str, Any]:
        """Preserve JWKS transport failures that FastMCP otherwise folds into None."""
        try:
            document = await super()._fetch_jwks()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise _AuthorizationServerUnavailable(str(exc)) from exc
        if not isinstance(document, dict):
            raise _AuthorizationServerUnavailable("JWKS response is not an object")
        keys = document.get("keys")
        if not isinstance(keys, list) or not keys:
            raise _AuthorizationServerUnavailable("JWKS contains no verification keys")
        usable_keys = []
        for key in keys:
            try:
                _jwk_to_pem(key)
            except Exception:
                continue
            usable_keys.append(key)
        if not usable_keys:
            raise _AuthorizationServerUnavailable("JWKS contains no usable verification keys")
        # Authorization servers may publish unsupported or malformed keys alongside
        # the RSA/EC keys this provider accepts. FastMCP converts every key in the
        # document, so remove unrelated keys rather than letting one disable every
        # otherwise-valid token.
        return {**document, "keys": usable_keys}


def _normalize_config_url(config_url: str) -> str:
    """Accept either a full metadata URL or a bare issuer; issuers get the OIDC
    well-known path appended (the common case for operators pasting an issuer)."""
    url = config_url.rstrip("/")
    if "/.well-known/" in urlsplit(url).path:
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
    if not isinstance(doc, dict):
        raise ValueError(f"AS metadata at {url} is not an object")
    issuer = doc.get("issuer")
    jwks_uri = doc.get("jwks_uri")
    if not runtime_settings.is_valid_oauth_endpoint_url(issuer):
        raise ValueError(f"AS metadata at {url} has an invalid issuer")
    if not runtime_settings.is_valid_oauth_endpoint_url(jwks_uri):
        raise ValueError(f"AS metadata at {url} has an invalid jwks_uri")
    _discovery_cache[url] = (now, doc)
    return doc


def _jwt_algorithm(token: str) -> str | None:
    try:
        encoded = token.split(".", 1)[0]
        padding = "=" * (-len(encoded) % 4)
        header = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    algorithm = header.get("alg") if isinstance(header, dict) else None
    return (
        algorithm
        if isinstance(algorithm, str) and algorithm in _ALLOWED_JWT_ALGORITHMS
        else None
    )


async def _verifier_for(config_url: str, audience: str, algorithm: str):
    """Build (or reuse) a JWTVerifier bound to the AS's issuer + JWKS."""
    doc = await _discovery(config_url)
    key = (config_url, audience, doc["jwks_uri"], doc["issuer"], algorithm)
    verifier = _verifier_cache.get(key)
    if verifier is None:
        verifier = _OAuthJWTVerifier(
            jwks_uri=doc["jwks_uri"],
            issuer=doc["issuer"],
            audience=audience,
            algorithm=algorithm,
        )
        _verifier_cache[key] = verifier
    return verifier


def _resource_kind(server: Server) -> str:
    return "g" if server.id.startswith("group:") else "s"


def _metadata_url(base: str, server: Server) -> str:
    # RFC 9728 §3: well-known inserted between host and the resource path.
    return f"{base}/.well-known/oauth-protected-resource/{_resource_kind(server)}/{server.slug}/mcp"


def _oauth_base_url(request: Request) -> str:
    """Use the public HTTPS scheme reported by a TLS-terminating proxy."""
    base = base_url(request)
    if not get_settings().public_base_url and base.startswith("http://"):
        from app.auth.middleware import is_loopback_client

        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded == "https" and is_loopback_client(request):
            base = "https://" + base[len("http://"):]
    return base


def _challenge(base: str, server: Server, error: Optional[str] = None) -> dict[str, str]:
    parts = ["Bearer"]
    if error:
        parts[0] += f' error="{error}",'
    parts.append(f'resource_metadata="{_metadata_url(base, server)}"')
    return {"WWW-Authenticate": " ".join(parts)}


def _identity_allowed(claims: dict[str, Any], allowed: list[str]) -> bool:
    """Match stable subjects exactly and friendly identity claims case-insensitively."""
    exact = set(allowed)
    subject = claims.get("sub")
    if subject is not None and str(subject) in exact:
        return True
    lowered = {value.lower() for value in allowed}
    return any(
        str(claims[key]).lower() in lowered
        for key in ("preferred_username", "login", "email")
        if claims.get(key)
    )


def _claims_are_current(claims: dict[str, Any]) -> bool:
    """Require a bounded JWT lifetime and honor an optional not-before claim."""
    now = time.time()
    exp = claims.get("exp")
    if (
        isinstance(exp, bool)
        or not isinstance(exp, (int, float))
        or not math.isfinite(exp)
        or exp <= now
    ):
        return False
    nbf = claims.get("nbf")
    if nbf is None:
        return True
    return (
        not isinstance(nbf, bool)
        and isinstance(nbf, (int, float))
        and math.isfinite(nbf)
        and nbf <= now + _CLOCK_SKEW_S
    )


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
        if not config_url or not audience:
            raise HTTPException(
                status_code=403,
                detail="oauth provider requires a config URL and audience",
            )

        base = _oauth_base_url(request)
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
                headers=_challenge(base, server),
            )
        algorithm = _jwt_algorithm(token)
        if algorithm is None:
            raise HTTPException(
                status_code=401,
                detail="invalid token",
                headers=_challenge(base, server, error="invalid_token"),
            )
        try:
            verifier = await _verifier_for(config_url, audience, algorithm)
            access = await verifier.verify_token(token)
        except HTTPException:
            raise
        except Exception as exc:
            # AS metadata/JWKS unreachable is OUR outage, not the client's: a 401
            # would send well-behaved clients into a pointless re-auth loop.
            logger.exception("authorization server unavailable")
            raise HTTPException(
                status_code=503, detail="authorization server unavailable"
            ) from exc
        if access is None:
            raise HTTPException(
                status_code=401,
                detail="invalid token",
                headers=_challenge(base, server, error="invalid_token"),
            )
        claims = access.claims
        if not _claims_are_current(claims):
            raise HTTPException(
                status_code=401,
                detail="invalid token",
                headers=_challenge(base, server, error="invalid_token"),
            )
        if allowed and not _identity_allowed(claims, allowed):
            raise HTTPException(
                status_code=403,
                detail="token subject not authorized for this server",
                headers=_challenge(base, server, error="insufficient_scope"),
            )


# --- Protected Resource Metadata (RFC 9728) -------------------------------------
# Public by design (clients fetch it pre-auth), so the router is mounted ungated.
# Only servers whose EFFECTIVE provider is ``oauth`` are advertised; everything
# else 404s so the endpoint doesn't leak which slugs exist.

wellknown = APIRouter()


def _effective_oauth(kind: str, slug: str) -> bool:
    with Session(get_engine()) as session:
        default = runtime_settings.default_auth_provider(session)
        config_url = runtime_settings.oauth_config_url(session)
        audience = runtime_settings.oauth_audience(session)
        if not config_url or not audience:
            return False
        if kind == "g":
            from app.groups import registry

            return registry.exists(session, slug) and default == "oauth"
        if kind != "s":
            return False
        server = repo.get_server_by_slug(session, slug)
        if server is None:
            return False
        name = server.auth_provider if server.auth_provider != "inherit" else default
        return name == "oauth"


@wellknown.get("/.well-known/oauth-protected-resource/{kind}/{slug}/mcp")
async def protected_resource_metadata(kind: str, slug: str, request: Request):
    # This endpoint is public for pre-auth discovery, but the independent
    # Host/Origin guard still applies before the request Host is used in metadata.
    from app.auth.middleware import enforce_host

    with Session(get_engine()) as session:
        enforce_host(request, session)
    if not _effective_oauth(kind, slug):
        raise HTTPException(status_code=404, detail="not found")
    with Session(get_engine()) as session:
        config_url = runtime_settings.oauth_config_url(session)
        scopes = runtime_settings.oauth_scopes(session)
    try:
        doc = await _discovery(config_url)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="authorization server metadata unavailable"
        ) from exc
    base = _oauth_base_url(request)
    meta = {
        "resource": f"{base}/{kind}/{slug}/mcp",
        "authorization_servers": [doc["issuer"]],
        "bearer_methods_supported": ["header"],
    }
    # Advertised scopes steer the client's authorize request (MCP clients read
    # scopes_supported from this document) — and therefore which claims the AS
    # puts in tokens. Without e.g. "profile", an AS like Authentik issues tokens
    # with no preferred_username and an identity allowlist can only match sub.
    if scopes:
        meta["scopes_supported"] = scopes
    return meta
