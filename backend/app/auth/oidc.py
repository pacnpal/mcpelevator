"""The single OpenID Connect provider this instance signs users in with.

This module talks to the identity provider. Settings live in ``app.registry.settings``
(the ``oidc_*`` keys); WHO a signed-in user is stays in ``app.auth.principal``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from sqlmodel import Session, select

from app.auth.oauth import _ALLOWED_JWT_ALGORITHMS
from app.db import repo
from app.db.models import User
from app.registry import settings as runtime_settings

logger = logging.getLogger(__name__)

# Every request to the identity provider goes through this transport. Tests swap in an
# ``httpx.MockTransport`` as a fake provider; None is httpx's real network transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

_TIMEOUT_S = 5.0
_REQUIRED = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")


def client() -> httpx.AsyncClient:
    """An HTTP client for the identity provider (no redirects, short timeout)."""
    return httpx.AsyncClient(transport=TRANSPORT, timeout=_TIMEOUT_S, follow_redirects=False)


async def check_settings(current: dict[str, Any], changes: dict[str, Any]) -> None:
    """Before a settings save: when it leaves SSO enabled and touches an ``oidc_*`` key,
    fetch the discovery document it would use. Raises ``ValueError`` naming the problem."""
    if not any(key.startswith("oidc_") for key in changes):
        return
    if not changes.get("oidc_enabled", current["oidc_enabled"]):
        return
    url = changes.get("oidc_discovery_url", current["oidc_discovery_url"])
    if not isinstance(url, str) or not url.strip():
        raise ValueError("set oidc_discovery_url before enabling SSO")
    await fetch_discovery(url.strip())


async def fetch_discovery(discovery_url: str) -> dict[str, Any]:
    """Fetch and check the provider's discovery document. ``discovery_url`` may be a bare
    issuer. Raises ``ValueError`` naming the problem."""
    url = runtime_settings.normalize_config_url(discovery_url)
    if not runtime_settings.is_valid_oauth_endpoint_url(url):
        raise ValueError(f"invalid oidc_discovery_url: {discovery_url!r}")
    try:
        async with client() as http:
            resp = await http.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("oidc discovery fetch from %s failed: %s", url, exc)
        raise ValueError(f"couldn't fetch the OIDC discovery document from {url}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"the OIDC discovery document at {url} is not a JSON object")
    bad = [k for k in _REQUIRED if not runtime_settings.is_valid_oauth_endpoint_url(doc.get(k))]
    if bad:
        raise ValueError(
            f"the OIDC discovery document at {url} lacks a valid {', '.join(bad)}"
        )
    return doc


# --------------------------------------------------------------------------- #
# Sign-in: authorization code + PKCE, then ID token validation
# --------------------------------------------------------------------------- #

# Failure reason codes for /login?sso=error&reason=<code>. Every one is a literal
# authored here, never provider text (the detail goes to the log).
PROVIDER_DENIED = "provider_denied"  # the person or the provider said no
PROVIDER_UNAVAILABLE = "provider_unavailable"  # discovery, token or JWKS fetch failed
PROVIDER_REJECTED = "provider_rejected"  # it refused our request, or its answer failed a check
NO_MATCH = "no_match"
AMBIGUOUS_MATCH = "ambiguous_match"
EXPIRED = "expired"  # unknown or expired state: start again
SSO_DISABLED = "sso_disabled"
MISSING_CLAIM = "missing_claim"  # the identity claim isn't among the provider's claims

_JWT_LEEWAY_S = 60
# Asymmetric only. EdDSA is Ed25519 (Pocket ID, Zitadel, Forgejo, Keycloak). RFC 9864's
# newer name "Ed25519" isn't a PyJWT algorithm yet, so a token naming it is refused.
_ID_TOKEN_ALGORITHMS = _ALLOWED_JWT_ALGORITHMS | {"EdDSA"}
_DEFAULT_SCOPES = ("openid", "email", "profile")
# Entra ID's multi-tenant discovery names its issuer with this placeholder; a token's
# ``iss`` carries the tenant's GUID (its ``tid`` claim) in its place.
_TENANT = "{tenantid}"
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# Google documents ``iss`` as either of these.
_GOOGLE = "https://accounts.google.com"
_UNAVAILABLE_ERRORS = frozenset({"server_error", "temporarily_unavailable"})
_DENIED_ERRORS = frozenset(
    {"access_denied", "consent_required", "login_required", "interaction_required",
     "account_selection_required"}
)


class SignInFailed(Exception):
    """A sign-in can't finish. ``reason`` is one of the codes above; the message is the
    log detail and never reaches the browser."""

    def __init__(self, reason: str, detail: str, claim: str | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.claim = claim  # for MISSING_CLAIM: the configured identity claim's name


def provider_error_reason(error: str) -> str:
    """Our code for the ``error`` the provider sent back to the callback."""
    code = error.strip().lower()
    if code in _UNAVAILABLE_ERRORS:
        return PROVIDER_UNAVAILABLE
    if code in _DENIED_ERRORS:
        return PROVIDER_DENIED
    return PROVIDER_REJECTED


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(
    discovery: dict[str, Any], *, client_id: str, redirect_uri: str, scopes: list[str],
    state: str, nonce: str, verifier: str,
) -> str:
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(dict.fromkeys([*_DEFAULT_SCOPES, *scopes])),
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    })
    endpoint = discovery["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


async def _discover(discovery_url: str) -> dict[str, Any]:
    try:
        return await fetch_discovery(discovery_url)
    except ValueError as exc:
        raise SignInFailed(PROVIDER_UNAVAILABLE, str(exc)) from exc


async def begin(session: Session) -> dict[str, Any]:
    """The discovery document for a sign-in about to start. Raises ``SignInFailed``."""
    values = runtime_settings.read_all(session)
    if not values["oidc_enabled"]:
        raise SignInFailed(SSO_DISABLED, "SSO is off")
    return await _discover(values["oidc_discovery_url"])


async def finish(
    session: Session, *, code: str, verifier: str, nonce: str, redirect_uri: str,
    iss: str | None,
) -> tuple[User, str]:
    """Exchange ``code``, validate the ID token, and match the person to a User, linking
    the Linked identity. Returns the user and the raw ID token. Raises ``SignInFailed``."""
    values = runtime_settings.read_all(session)
    discovery = await begin(session)
    issuer = discovery["issuer"]
    if iss is not None and _concrete_issuer(issuer, iss) is None:  # RFC 9207: a mix-up
        raise SignInFailed(PROVIDER_REJECTED, f"callback iss {iss!r} is not {issuer!r}")
    client_id = values["oidc_client_id"]
    try:
        secret = runtime_settings.oidc_client_secret(session)
    except ValueError as exc:
        raise SignInFailed(PROVIDER_REJECTED, str(exc)) from exc
    async with client() as http:
        tokens = await _exchange(http, discovery, client_id, secret, code, verifier, redirect_uri)
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise SignInFailed(PROVIDER_REJECTED, "the token response carried no id_token")
        claims = await _validate(http, discovery, id_token, client_id=client_id, nonce=nonce)
        claims = await _with_userinfo(http, discovery, tokens.get("access_token"), claims)
    return _match(session, claims, claims["iss"], values["oidc_identity_claim"]), id_token


def _concrete_issuer(template: str, iss: str, tid: str | None = None) -> str | None:
    """The issuer a token's (or callback's) ``iss`` names under the discovery issuer
    ``template``, or None when it doesn't fit. Entra ID's ``{tenantid}`` must be the
    token's ``tid``, a GUID (any GUID when ``tid`` is None, as on the callback).
    Google's scheme-less ``accounts.google.com`` comes back in its https form."""
    if template == _GOOGLE and iss == "accounts.google.com":
        return _GOOGLE
    if _TENANT not in template:
        return iss if iss == template else None
    if tid is None:
        pattern = re.escape(template).replace(re.escape(_TENANT), _GUID)
        return iss if re.fullmatch(pattern, iss) else None
    if not re.fullmatch(_GUID, tid):
        return None
    return iss if iss == template.replace(_TENANT, tid) else None


async def _exchange(
    http: httpx.AsyncClient, discovery: dict[str, Any], client_id: str, secret: str,
    code: str, verifier: str, redirect_uri: str,
) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "code_verifier": verifier, "client_id": client_id,
    }
    auth = None
    if secret:
        methods = discovery.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in methods:
            auth = httpx.BasicAuth(client_id, secret)
        else:
            form["client_secret"] = secret
    try:
        resp = await http.post(discovery["token_endpoint"], data=form, auth=auth)
    except httpx.HTTPError as exc:
        raise SignInFailed(PROVIDER_UNAVAILABLE, f"token endpoint: {exc}") from exc
    if resp.status_code >= 500:
        raise SignInFailed(PROVIDER_UNAVAILABLE, f"token endpoint answered {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code != 200 or not isinstance(body, dict):
        error = body.get("error", "") if isinstance(body, dict) else ""
        raise SignInFailed(
            PROVIDER_REJECTED, f"token endpoint answered {resp.status_code} {error!r}"
        )
    return body


async def _validate(
    http: httpx.AsyncClient, discovery: dict[str, Any], id_token: str, *, client_id: str,
    nonce: str,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SignInFailed(PROVIDER_REJECTED, f"unreadable id_token: {exc}") from exc
    alg = header.get("alg")
    if alg not in _ID_TOKEN_ALGORITHMS:
        if isinstance(alg, str) and alg.upper().startswith("HS"):
            # Authentik's default when no signing key is chosen for the provider.
            logger.warning(
                "the identity provider signed the ID token with a shared secret (%s); "
                "select a signing key for this client in the provider", alg,
            )
        raise SignInFailed(PROVIDER_REJECTED, f"id_token alg {alg!r} not allowed")
    try:
        resp = await http.get(discovery["jwks_uri"], headers={"accept": "application/json"})
        resp.raise_for_status()
        jwks = resp.json()
        keys = jwt.PyJWKSet.from_dict(jwks).keys
    except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
        raise SignInFailed(PROVIDER_UNAVAILABLE, f"JWKS fetch: {exc}") from exc
    kid = header.get("kid")
    candidates = [k for k in keys if kid is None or k.key_id == kid]
    if len(candidates) != 1:
        raise SignInFailed(PROVIDER_REJECTED, f"no single JWKS key for kid {kid!r}")
    try:
        claims = jwt.decode(
            id_token, candidates[0], algorithms=sorted(_ID_TOKEN_ALGORITHMS),
            audience=client_id, leeway=_JWT_LEEWAY_S,
            options={"require": ["iss", "aud", "sub", "exp", "iat"]},
        )  # iss is checked below, against the discovery issuer's template
    except jwt.PyJWTError as exc:
        raise SignInFailed(PROVIDER_REJECTED, f"id_token: {exc}") from exc
    iss, tid = claims["iss"], claims.get("tid")
    tid = tid if isinstance(tid, str) else ""
    concrete = _concrete_issuer(discovery["issuer"], iss, tid) if isinstance(iss, str) else None
    if concrete is None:
        raise SignInFailed(PROVIDER_REJECTED, f"id_token iss {iss!r} is not {discovery['issuer']!r}")
    # Entra ID's keys name the issuer they sign for; it must fit the token's too.
    raw = next((k for k in jwks["keys"] if isinstance(k, dict) and k.get("kid") == kid), {})
    key_issuer = raw.get("issuer")
    if key_issuer is not None and (
        not isinstance(key_issuer, str) or _concrete_issuer(key_issuer, iss, tid) is None
    ):
        raise SignInFailed(PROVIDER_REJECTED, f"JWKS key issuer {key_issuer!r} is not {iss!r}")
    claims["iss"] = concrete
    aud = claims["aud"]
    several = isinstance(aud, list) and len(aud) > 1
    if ("azp" in claims or several) and claims.get("azp") != client_id:
        raise SignInFailed(PROVIDER_REJECTED, f"id_token azp {claims.get('azp')!r} is not ours")
    if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
        raise SignInFailed(PROVIDER_REJECTED, "id_token nonce doesn't match this sign-in")
    return claims


async def _with_userinfo(
    http: httpx.AsyncClient, discovery: dict[str, Any], access_token: Any,
    claims: dict[str, Any],
) -> dict[str, Any]:
    """``claims`` plus the userinfo endpoint's, which are taken only when its ``sub`` is
    the ID token's. ID token claims win a clash."""
    endpoint = discovery.get("userinfo_endpoint")
    if not endpoint or not isinstance(access_token, str):
        return claims
    try:
        resp = await http.get(endpoint, headers={"authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        info = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("oidc userinfo fetch failed, using the ID token alone: %s", exc)
        return claims
    if not isinstance(info, dict) or info.get("sub") != claims["sub"]:
        raise SignInFailed(PROVIDER_REJECTED, "userinfo sub differs from the ID token's")
    return {**info, **claims}


def _match(session: Session, claims: dict[str, Any], issuer: str, claim: str) -> User:
    """The one User whose name equals the identity claim (case-insensitively), linked to
    this issuer and subject. Name match only for now; #269 adds Linked-identity-first
    matching, the recycled-email and issuer rules, and the write lock."""
    value = claims.get(claim)
    if not isinstance(value, str) or not value.strip():
        raise SignInFailed(MISSING_CLAIM, f"the {claim} claim is missing", claim=claim)
    wanted = value.strip().casefold()
    matches = [u for u in repo.list_users(session) if u.name.strip().casefold() == wanted]
    if not matches:
        raise SignInFailed(NO_MATCH, f"no user is named {value!r}")
    if len(matches) > 1:
        raise SignInFailed(AMBIGUOUS_MATCH, f"{len(matches)} users are named {value!r}")
    user = matches[0]
    subject = str(claims["sub"])
    holder = session.exec(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    ).first()
    if holder is not None and holder.id != user.id:
        raise SignInFailed(AMBIGUOUS_MATCH, "another user already holds this Linked identity")
    if (user.oidc_issuer, user.oidc_subject) != (issuer, subject):
        user.oidc_issuer, user.oidc_subject = issuer, subject
        user = repo.save_user(session, user)
    return user
