"""The single OpenID Connect provider this instance signs users in with.

This module talks to the identity provider. Settings live in ``app.registry.settings``
(the ``oidc_*`` keys); WHO a signed-in user is stays in ``app.auth.principal``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, urlencode

import httpx
import jwt
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update
from starlette.requests import Request

from app import sealed
from app.api.users import change_user
from app.auth import jwks, sign_in
from app.auth import oauth as oauth_mod
from app.auth.oauth import _ALLOWED_JWT_ALGORITHMS, verified_email
from app.auth.oauth_flow import log_safe
from app.db import repo
from app.db.models import SignInSession, User
from app.registry import service
from app.registry import settings as runtime_settings
from app.util import new_id

logger = logging.getLogger(__name__)

# Every request to the identity provider goes through this transport. Tests swap in an
# ``httpx.MockTransport`` as a fake provider; None is httpx's real network transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

_TIMEOUT_S = 5.0
# The last discovery document fetched per (normalized) discovery URL. Logout reads it and
# never fetches: after a restart it's empty until the next sign-in or settings save.
_DISCOVERY: dict[str, dict[str, Any]] = {}
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
    doc = await fetch_discovery(url.strip())
    tenants = changes.get("oidc_allowed_tenants", current["oidc_allowed_tenants"])
    if _TENANT in doc["issuer"] and not tenants:
        raise ValueError(
            "this provider is multi-tenant (its issuer contains {tenantid}): a tenant list "
            "is required, set oidc_allowed_tenants to the tenant GUIDs allowed to sign in"
        )


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
    _DISCOVERY[url] = doc
    return doc


def logout_url(session: Session, *, id_token: str, post_logout_redirect_uri: str) -> str | None:
    """The provider's RP-initiated logout URL, or None when the cached discovery document
    (no fetch here) advertises no ``end_session_endpoint``. ``id_token`` may be empty, and
    then only ``client_id`` identifies us."""
    values = runtime_settings.read_all(session)
    doc = _DISCOVERY.get(runtime_settings.normalize_config_url(values["oidc_discovery_url"]))
    endpoint = (doc or {}).get("end_session_endpoint")
    if not runtime_settings.is_valid_oauth_endpoint_url(endpoint):
        return None
    query = urlencode({
        **({"id_token_hint": id_token} if id_token else {}),
        "client_id": values["oidc_client_id"],
        "post_logout_redirect_uri": post_logout_redirect_uri,
    })
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


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
TENANT_NOT_ALLOWED = "tenant_not_allowed"  # a multi-tenant issuer, a tenant not listed
UNVERIFIED_EMAIL = "unverified_email"  # the identity claim is email and email_verified is false
IDENTITY_CONFLICT = "identity_conflict"  # the matched User is linked to another identity
NOT_ALLOWED = "not_allowed"  # in none of oidc_allowed_groups
GROUPS_OVERAGE = "groups_overage"  # Entra left the groups out (too many): can't tell
DOMAIN_NOT_ALLOWED = "domain_not_allowed"  # no verified email domain or hd in the list
DEACTIVATED = "deactivated"  # the matched User is a Deactivated user
# The groups would demote the last admin login: the role stays, the sign-in is refused
LAST_ADMIN_DEMOTED = "last_admin_demoted"

_JWT_LEEWAY_S = 60
# Asymmetric only. EdDSA is Ed25519 (Pocket ID, Zitadel, Forgejo, Keycloak). RFC 9864's
# newer name "Ed25519" isn't a PyJWT algorithm yet, so a token naming it is refused.
_ID_TOKEN_ALGORITHMS = _ALLOWED_JWT_ALGORITHMS | {"EdDSA"}
_DEFAULT_SCOPES = ("openid", "email", "profile")
_GROUP_LISTS = ("oidc_allowed_groups", "oidc_admin_groups", "oidc_local_runner_groups")
# Settings that decide access from the provider's claims: a refresh must see fresh ones.
_ACCESS_POLICY = (*_GROUP_LISTS, "oidc_allowed_domains", "oidc_allowed_tenants")
# What a userinfo fetch gave (``_with_userinfo``).
USERINFO_FRESH, USERINFO_OUTAGE, USERINFO_NONE = "fresh", "outage", "none"
# Entra ID's multi-tenant discovery names its issuer with this placeholder; a token's
# ``iss`` carries the tenant's GUID (its ``tid`` claim) in its place.
_TENANT = "{tenantid}"
_GUID = runtime_settings.GUID_PATTERN
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
    # Google hands out a refresh token for access_type=offline, not the offline_access
    # scope (requested_scopes leaves that out for Google).
    offline = {"access_type": "offline"} if discovery["issuer"] == _GOOGLE else {}
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(dict.fromkeys([*_DEFAULT_SCOPES, *scopes])),
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        **offline,
    })
    endpoint = discovery["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


def requested_scopes(values: dict[str, Any], discovery: dict[str, Any]) -> list[str]:
    """The extra scopes a sign-in asks for: ``oidc_scopes``, plus ``groups`` when a group
    list is in use and the provider lists that scope (Authelia, Pocket ID, Gitea and
    Zitadel send groups only when it's asked for), and ``offline_access`` when the
    provider lists it (Entra ID, Okta, Authentik, Authelia and Zitadel issue a refresh
    token only for that scope). Google gets ``access_type=offline`` instead."""
    scopes = list(values["oidc_scopes"])
    supported = discovery.get("scopes_supported")
    supported = supported if isinstance(supported, list) else []
    if any(values[key] for key in _GROUP_LISTS) and "groups" in supported:
        scopes.append("groups")
    if "offline_access" in supported and discovery["issuer"] != _GOOGLE:
        scopes.append("offline_access")
    return scopes


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
    iss: str | None, bound: str,
) -> tuple[User, sign_in.Grant]:
    """Exchange ``code``, validate the ID token, and match the person to a User, linking
    the Linked identity and syncing provider groups. Every decision uses one settings
    snapshot: the current one, refused unless its digest is ``bound`` (the one the
    login began with, ``settings.sign_in_digest``), so equal settings; nothing is
    written unless it still holds under the write lock. Returns the
    user and what the session keeps of the provider's tokens. Raises ``SignInFailed``."""
    values = runtime_settings.read_all(session)
    if runtime_settings.sign_in_digest(values) != bound:
        raise SignInFailed(EXPIRED, "the SSO settings changed since this login began")
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
        tokens = await _token(http, discovery, client_id, secret, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        })
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise SignInFailed(PROVIDER_REJECTED, "the token response carried no id_token")
        claims = await _validate(
            http, discovery, id_token, client_id=client_id, nonce=nonce,
            allowed_tenants=values["oidc_allowed_tenants"],
        )
        claims, _ = await _with_userinfo(http, discovery, tokens.get("access_token"), claims)
    groups = _decide(values, claims)
    # Threadpool: the config write lock is a threading lock, never waited for on the loop.
    linked = await run_in_threadpool(_link, session, claims, values, groups, bound)
    return linked, _grant(tokens, id_token, claims, "", nonce=nonce)


def _grant(
    tokens: dict[str, Any], id_token: str, claims: dict[str, Any], refresh_token: str, *,
    nonce: str = "", fallback: datetime | None = None,
) -> sign_in.Grant:
    """What the session keeps of a token response. A new refresh token replaces
    ``refresh_token`` (Authelia and others rotate single-use ones), and the access
    token runs out after ``expires_in``, else at ``fallback`` when given (a refresh
    with no new ID token), else at the ID token's ``exp``."""
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires = datetime.now(timezone.utc) + timedelta(seconds=max(0, min(expires_in, 10**9)))
    elif fallback is not None:
        expires = fallback
    else:
        expires = datetime.fromtimestamp(claims["exp"], timezone.utc)
    new = tokens.get("refresh_token")
    return sign_in.Grant(
        id_token=id_token,
        refresh_token=new if isinstance(new, str) and new else refresh_token,
        access_expires_at=expires,
        nonce=nonce,
    )


def _decide(values: dict[str, Any], claims: dict[str, Any]) -> set[str]:
    """The verified-email rule, then ``_gate``: the person's groups. Raises
    ``SignInFailed``."""
    claim = values["oidc_identity_claim"]
    if claim == "email" and values["oidc_require_verified_email"] and not _email_verified(claims):
        raise SignInFailed(
            UNVERIFIED_EMAIL, f"email_verified is {claims.get('email_verified')!r}"
        )
    return _gate(values, claims)


# --------------------------------------------------------------------------- #
# Session refresh: the refresh_token grant, then the sign-in checks again
# --------------------------------------------------------------------------- #

# One refresh at a time per Sign-in session id; an entry lives while a refresh holds it.
_REFRESH_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
# After a provider outage the session counts this much longer before the next try.
_RETRY_AFTER = timedelta(minutes=1)
# A refresh that says neither when the access token runs out (expires_in) nor brings a
# new ID token: the session counts this long before the next refresh.
_FALLBACK_LIFETIME = timedelta(minutes=5)


async def refresh(session: Session, row: SignInSession) -> None:
    """Refresh the soft-expired Sign-in session ``row`` against the provider. On success
    it is live again with the provider's new tokens, its groups mapped again exactly as
    at sign-in. Access is judged only on fresh claims: the new ID token, else userinfo.
    A refusal (``invalid_grant``, another subject, a Deactivated user, a gate, the
    last-admin guard, changed sign-in settings, no fresh claims while an access policy
    is set) ends it. A provider outage keeps it for ``_RETRY_AFTER`` more, never past
    its hard expiry, with any refresh token the provider rotated before it."""
    session_id = row.id
    lock = _REFRESH_LOCKS.setdefault(session_id, asyncio.Lock())
    async with lock:
        session.expire_all()
        row = session.get(SignInSession, session_id)
        if row is None or not sign_in.soft_expired(row):
            return  # ended, or refreshed by the request this one waited for
        hard = sign_in.hard_expiry(row)
        try:
            await _refresh(session, row)
        except SignInFailed as exc:
            session.rollback()
            detail = log_safe(str(exc))
            if exc.reason == PROVIDER_UNAVAILABLE:
                logger.info("SSO session refresh failed, trying again later: %s", detail)
                retry = min(datetime.now(timezone.utc) + _RETRY_AFTER, hard)
                session.execute(
                    update(SignInSession).where(SignInSession.id == session_id)
                    .values(soft_expires_at=retry)
                )
            else:
                logger.warning(
                    "SSO session refresh refused (%s), session ended: %s", exc.reason, detail
                )
                repo.delete_sign_in_session(session, session_id)
            session.commit()


async def _refresh(session: Session, row: SignInSession) -> None:
    # Read now: the row may go mid-refresh.
    session_id, bound, hard = row.id, row.settings, sign_in.hard_expiry(row)
    values = runtime_settings.read_all(session)
    if not values["oidc_enabled"] or runtime_settings.sign_in_digest(values) != bound:
        raise SignInFailed(EXPIRED, "the SSO settings changed since this session began")
    cached = _DISCOVERY.get(runtime_settings.normalize_config_url(values["oidc_discovery_url"]))
    discovery = cached or await _discover(values["oidc_discovery_url"])
    client_id = values["oidc_client_id"]
    try:
        secret = runtime_settings.oidc_client_secret(session)
    except ValueError as exc:
        raise SignInFailed(PROVIDER_REJECTED, str(exc)) from exc
    stored, refresh_token = sealed.unseal(row.id_token), sealed.unseal(row.refresh_token)
    if not stored or not refresh_token:
        raise SignInFailed(PROVIDER_REJECTED, "the session's tokens can't be read")
    try:  # validated when it was stored; read here for its issuer and subject
        old = jwt.decode(stored, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise SignInFailed(PROVIDER_REJECTED, f"stored id_token: {exc}") from exc
    nonce = row.nonce or str(old.get("nonce", ""))  # a session from before the column
    async with client() as http:
        tokens = await _token(http, discovery, client_id, secret, {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
        })
        rotated = tokens.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != refresh_token:
            # The provider may have spent the old one: keep the new one now, so an outage
            # in the checks below leaves a token the next try can use. Nothing else
            # lands until they pass.
            session.execute(
                update(SignInSession).where(SignInSession.id == session_id)
                .values(refresh_token=sealed.seal(rotated))
            )
            session.commit()
            refresh_token = rotated
        id_token = tokens.get("id_token")
        fresh_id_token = isinstance(id_token, str) and bool(id_token)
        if fresh_id_token:
            claims = await _validate(
                http, discovery, id_token, client_id=client_id,
                nonce=nonce, allowed_tenants=values["oidc_allowed_tenants"],
                refreshed=True,
            )
        else:  # no new ID token: the stored one vouches for who this is, never for access
            id_token = stored
            identity = {k: old[k] for k in ("iss", "sub", "tid") if k in old}
            iss, tid = identity.get("iss"), identity.get("tid")
            concrete = _concrete_issuer(
                discovery["issuer"], iss, tid if isinstance(tid, str) else ""
            ) if isinstance(iss, str) else None
            if concrete is None or "sub" not in identity:
                raise SignInFailed(PROVIDER_REJECTED, f"stored id_token iss {iss!r} doesn't fit")
            identity["iss"] = concrete
            claims = identity
        claims, userinfo = await _with_userinfo(
            http, discovery, tokens.get("access_token"), claims
        )
    if userinfo == USERINFO_OUTAGE:
        # Groups may live in userinfo alone: missing ones aren't a removal. Keep the
        # session as it is and try again, like any other outage.
        raise SignInFailed(PROVIDER_UNAVAILABLE, "userinfo is down")
    if not fresh_id_token and userinfo != USERINFO_FRESH and any(
        values[k] for k in _ACCESS_POLICY
    ):
        # The provider answered, but neither a new ID token nor userinfo: the groups,
        # domain and tenant can't be checked again, so the person signs in again.
        raise SignInFailed(
            PROVIDER_REJECTED, "the refresh brought no fresh claims to check access with"
        )
    groups = _decide(values, claims)
    fallback = None if fresh_id_token else min(
        datetime.now(timezone.utc) + _FALLBACK_LIFETIME, hard
    )
    grant = _grant(tokens, id_token, claims, refresh_token, fallback=fallback)
    await run_in_threadpool(
        _link, session, claims, values, groups, bound, (session_id, grant)
    )


def _gate(values: dict[str, Any], claims: dict[str, Any]) -> set[str]:
    """The person's groups, once the allowed-groups and allowed-domains gates pass.
    Raises ``SignInFailed``."""
    name = values["oidc_groups_claim"]
    overage = claims.get("_claim_names")
    in_use = any(values[key] for key in _GROUP_LISTS)
    if in_use and isinstance(overage, dict) and (name in overage or "groups" in overage):
        # Entra moved the groups to a Graph call we don't make: we can't tell who
        # this person is allowed to be, so fail closed.
        raise SignInFailed(GROUPS_OVERAGE, f"_claim_names carries {name!r}: groups overage")
    groups = _groups(claims, name)
    allowed = values["oidc_allowed_groups"]
    if allowed and not groups.intersection(allowed):
        raise SignInFailed(NOT_ALLOWED, f"in none of oidc_allowed_groups ({len(groups)} groups)")
    domains = values["oidc_allowed_domains"]
    if domains:
        hd = claims.get("hd")
        if isinstance(hd, str) and hd:
            domain = hd.lower()
        else:  # an authorization decision: only an explicitly verified email counts
            email = verified_email(claims)
            domain = email.rsplit("@", 1)[1].lower() if email and "@" in email else None
        if domain not in domains:
            raise SignInFailed(DOMAIN_NOT_ALLOWED, f"domain {domain!r} is not in oidc_allowed_domains")
    return groups


def _groups(claims: dict[str, Any], name: str) -> set[str]:
    """The groups claim ``name``: a literal key first (Auth0's URL-shaped names hold
    dots), then a dotted path (Keycloak's ``realm_access.roles``). A list keeps its
    strings, a string is one group, an object counts its keys (Zitadel's roles);
    anything else, or no claim, is no groups."""
    value: Any = claims.get(name)
    if name not in claims:
        value = claims
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    if isinstance(value, dict):
        return {k for k in value if isinstance(k, str)}
    return set()


def _link(
    session: Session, claims: dict[str, Any], values: dict[str, Any], groups: set[str],
    bound: str, refreshing: tuple[str, sign_in.Grant] | None = None,
) -> User:
    """``_match``, the group sync and the provider recheck as ONE transaction under the
    config write lock, so they are one step against other sign-ins, an admin's unlink
    and the last-admin guard. The link and sync are flushed first, which takes the
    database write lock a settings write also needs; then the sign-in settings are
    read again, and only if they are still ``bound`` (the snapshot ``values`` came
    from) does anything commit. A demotion the last-admin guard blocks refuses the
    sign-in and writes nothing: the role stays, so the guard holds. A writer
    outside the config lock (another instance on this database) can still link the
    pair first: the unique index refuses ours, and the pair is resolved again.

    ``refreshing`` is (session id, grant) for a session refresh: the pair must still be
    linked to that session's User (no matching, no linking), and the session takes the
    grant in the same commit."""
    with service.config_write_lock():
        session.expire_all()  # see what committed while we waited
        if refreshing is not None:
            row = session.get(SignInSession, refreshing[0])
            user = _holder(session, claims["iss"], str(claims["sub"]))
            if row is None:
                raise SignInFailed(EXPIRED, "the session ended while it refreshed")
            if user is None or user.id != row.user_id:
                raise SignInFailed(
                    IDENTITY_CONFLICT, "the refreshed ID token names another identity"
                )
        else:
            user = _link_user(session, claims, values)
        if not user.active:  # whatever the provider said; only an admin brings them back
            session.rollback()
            raise SignInFailed(DEACTIVATED, f"{user.name!r} is a deactivated user")
        name = user.name
        synced = _sync(session, user, groups, values)
        session.expire_all()  # the settings as committed, not as this session cached them
        if runtime_settings.sign_in_settings(session) != bound:
            session.rollback()
            raise SignInFailed(EXPIRED, "the SSO settings changed while this login finished")
        if isinstance(synced, str):  # "last_admin"; the callback logs the one warning
            session.rollback()
            raise SignInFailed(
                LAST_ADMIN_DEMOTED,
                f"the groups would demote {name!r}, the last admin login: the role stays admin",
            )
        user = synced
        if refreshing is not None:
            session.execute(
                update(SignInSession).where(SignInSession.id == refreshing[0])
                .values(**refreshing[1].columns())
            )
        session.commit()
        session.refresh(user)  # the pair as linked, before anything else can change it
        return user


def _link_user(session: Session, claims: dict[str, Any], values: dict[str, Any]) -> User:
    """``_match`` for a sign-in, resolving a lost link race by its pair."""
    try:
        return _match(
            session, claims, claims["iss"], values["oidc_identity_claim"],
            provision=values["oidc_provisioning"],
        )
    except IntegrityError:
        session.rollback()
        user = _holder(session, claims["iss"], str(claims["sub"]))
        if user is None:  # couldn't tell who this is: fail closed
            raise SignInFailed(IDENTITY_CONFLICT, "the link write lost a race") from None
        return user


def _sync(session: Session, user: User, groups: set[str], values: dict[str, Any]) -> User | str:
    """Set the fields provider groups manage under ``values``, through the same
    mutation PATCH /users uses (so a demotion sweeps the admin-only tokens), flushed
    for ``_link`` to commit. Returns "last_admin" when the demotion would leave no
    admin login."""
    admin_groups, runner_groups = values["oidc_admin_groups"], values["oidc_local_runner_groups"]
    role = ("admin" if groups.intersection(admin_groups) else "member") if admin_groups else None
    runners = bool(groups.intersection(runner_groups)) if runner_groups else None
    if role is None and runners is None:
        return user
    return change_user(session, user, role=role, local_runners=runners, commit=False)


def _holder(session: Session, issuer: str, subject: str) -> User | None:
    return session.exec(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    ).first()


def _email_verified(claims: dict[str, Any]) -> bool:
    """``email_verified`` as a boolean, for matching the identity claim only. Absent
    counts as verified; "true"/"false" in any case are read as booleans; any other value
    counts as unverified (fail closed). The domain gate grants access, so it uses the
    strict ``verified_email`` instead."""
    value = claims.get("email_verified", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return False


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


async def _token(
    http: httpx.AsyncClient, discovery: dict[str, Any], client_id: str, secret: str,
    form: dict[str, str],
) -> dict[str, Any]:
    """POST ``form`` (a grant) to the token endpoint with our client authentication.
    A transport failure or a 5xx is PROVIDER_UNAVAILABLE; any other refusal is
    PROVIDER_REJECTED."""
    form = {**form, "client_id": client_id}
    auth = None
    if secret:
        methods = discovery.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in methods:
            # RFC 6749 §2.3.1: form-encode each half before the Basic pair.
            auth = httpx.BasicAuth(quote_plus(client_id), quote_plus(secret))
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
        if error in _UNAVAILABLE_ERRORS:  # RFC 6749 4.1.2.1: the provider is busy, not saying no
            raise SignInFailed(
                PROVIDER_UNAVAILABLE, f"token endpoint answered {resp.status_code} {error!r}"
            )
        raise SignInFailed(
            PROVIDER_REJECTED, f"token endpoint answered {resp.status_code} {error!r}"
        )
    return body


async def _validate(
    http: httpx.AsyncClient, discovery: dict[str, Any], id_token: str, *, client_id: str,
    nonce: str, allowed_tenants: list[str], refreshed: bool = False,
) -> dict[str, Any]:
    """The ID token's claims once every check passes. ``refreshed``: it came from a
    refresh, where OIDC Core 12.2 lets it leave out the nonce; one it carries must
    still be the sign-in's."""
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
    # A multi-tenant issuer fits every Entra tenant: only the listed ones may sign in
    # (an empty list admits none), so another tenant's account can't claim a User here.
    if _TENANT in discovery["issuer"] and tid.lower() not in allowed_tenants:
        raise SignInFailed(TENANT_NOT_ALLOWED, f"tenant {tid!r} is not in oidc_allowed_tenants")
    claims["iss"] = concrete
    aud = claims["aud"]
    several = isinstance(aud, list) and len(aud) > 1
    if ("azp" in claims or several) and claims.get("azp") != client_id:
        raise SignInFailed(PROVIDER_REJECTED, f"id_token azp {claims.get('azp')!r} is not ours")
    if (not refreshed or "nonce" in claims) and not hmac.compare_digest(
        str(claims.get("nonce", "")).encode(), nonce.encode()
    ):
        raise SignInFailed(PROVIDER_REJECTED, "id_token nonce doesn't match this sign-in")
    return claims


async def _with_userinfo(
    http: httpx.AsyncClient, discovery: dict[str, Any], access_token: Any,
    claims: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """``claims`` plus the userinfo endpoint's, which are taken only when its ``sub`` is
    the ID token's. ID token claims win a clash, except that ``email`` and
    ``email_verified`` come as a pair from one response (#291): the ID token's when it
    has an email, userinfo's when it has none. Userinfo's ``email_verified`` counts for
    the ID token's email only when userinfo sends that same email and the ID token
    states no verification of its own. Also returns what userinfo gave: ``USERINFO_FRESH``
    (its claims are merged), ``USERINFO_OUTAGE`` (no answer or a 5xx) or
    ``USERINFO_NONE`` (no endpoint, no access token, or an answer without claims)."""
    endpoint = discovery.get("userinfo_endpoint")
    if not endpoint or not isinstance(access_token, str):
        return claims, USERINFO_NONE
    try:
        resp = await http.get(endpoint, headers={"authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        logger.info("oidc userinfo fetch failed, using the ID token alone: %s", exc)
        return claims, USERINFO_OUTAGE
    if resp.status_code >= 500:
        logger.info("oidc userinfo answered %s, using the ID token alone", resp.status_code)
        return claims, USERINFO_OUTAGE
    try:
        resp.raise_for_status()
        info = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("oidc userinfo refused, using the ID token alone: %s", exc)
        return claims, USERINFO_NONE
    if not isinstance(info, dict) or info.get("sub") != claims["sub"]:
        raise SignInFailed(PROVIDER_REJECTED, "userinfo sub differs from the ID token's")
    merged = {**info, **claims}
    if "email" not in claims:
        pair = info
    elif info.get("email") == claims["email"] and "email_verified" not in claims:
        pair = info
    else:
        pair = claims
    merged.pop("email_verified", None)
    if "email_verified" in pair:
        merged["email_verified"] = pair["email_verified"]
    return merged, USERINFO_FRESH


def _match(
    session: Session, claims: dict[str, Any], issuer: str, claim: str, *, provision: bool,
) -> User:
    """The User already linked to this issuer and subject, else the one User whose name
    equals the identity claim (case-insensitively) and who has no Linked identity yet,
    which is then linked. With ``provision`` and no name match, a new User named by the
    claim: a Member with local runners off, linked at creation. A name match on a User
    linked to anything else is refused: a provider that hands an email or username to a
    new subject must not hand it our User too. Moving to a new issuer goes through an
    admin's unlink."""
    subject = str(claims["sub"])
    holder = _holder(session, issuer, subject)
    if holder is not None:
        return holder
    value = claims.get(claim)
    if not isinstance(value, str) or not value.strip():
        raise SignInFailed(MISSING_CLAIM, f"the {claim} claim is missing", claim=claim)
    matches = repo.users_named(session, value)
    if not matches and provision:
        user = User(
            id=new_id(), name=value.strip(), role="member", local_runners=False,
            control_mcp=True, oidc_issuer=issuer, oidc_subject=subject,
        )
        return repo.save_user(session, user, commit=False)  # _link commits
    if not matches:
        raise SignInFailed(NO_MATCH, f"no user is named {value!r}")
    if len(matches) > 1:
        raise SignInFailed(AMBIGUOUS_MATCH, f"{len(matches)} users are named {value!r}")
    user = matches[0]
    if user.oidc_issuer or user.oidc_subject:
        raise SignInFailed(
            IDENTITY_CONFLICT,
            f"{user.name!r} is already linked to {user.oidc_issuer!r}, not subject {subject!r}",
        )
    user.oidc_issuer, user.oidc_subject = issuer, subject
    return repo.save_user(session, user, commit=False)  # _link commits


# --------------------------------------------------------------------------- #
# Provider access tokens: one identity across planes
# --------------------------------------------------------------------------- #


def _both_configured(values: dict[str, Any]) -> bool:
    return bool(values["oidc_enabled"] and values["oauth_config_url"] and values["oauth_audience"])


async def warm_access_tokens(session: Session, request: Request) -> None:
    """Fetch the discovery documents and signing keys ``access_token_user`` reads from
    cache, when the request carries a bearer that could be an access token and SSO and
    the ``oauth`` provider are both configured. The keys load off the loop, so the first
    token after a restart doesn't meet an empty cache. Async callers run it before a
    sync resolution; a failed fetch is logged, and then no access token resolves."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip() or token.strip().startswith("mcpe_"):
        return
    values = runtime_settings.read_all(session)
    if not _both_configured(values):
        return
    try:
        oauth_doc = await oauth_mod._discovery(values["oauth_config_url"])
        if runtime_settings.normalize_config_url(values["oidc_discovery_url"]) not in _DISCOVERY:
            await fetch_discovery(values["oidc_discovery_url"])
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("discovery for provider access tokens failed: %s", exc)
        return
    try:
        await asyncio.to_thread(jwks.signing_key, oauth_doc["jwks_uri"], token.strip())
    except jwt.PyJWKClientConnectionError as exc:
        logger.info("signing keys for provider access tokens failed: %s", exc)
    except jwt.PyJWTError:
        pass  # no key for this token: access_token_user refuses it



def access_token_user(session: Session, token: str) -> User | None:
    """The User a provider access token names through their Linked identity, or None.

    It counts only when SSO and the ``oauth`` provider trust the same issuer. The token
    is verified with the ``oauth`` settings (its discovery's keys, ``oauth_audience``):
    asymmetric algorithms only, ``exp`` with 60s leeway, and an ``iss`` that fits both
    discovery issuers (an Entra ``{tenantid}`` must be the token's ``tid``, a listed
    tenant). Sync, for the event loop: discovery comes from cache (``warm_access_tokens``)
    and keys from ``app.auth.jwks``. It only reads: no provisioning, linking or groups."""
    if not token or token.startswith("mcpe_"):
        return None
    values = runtime_settings.read_all(session)
    if not _both_configured(values):
        return None
    sso = _DISCOVERY.get(runtime_settings.normalize_config_url(values["oidc_discovery_url"]))
    cached = oauth_mod._discovery_cache.get(
        runtime_settings.normalize_config_url(values["oauth_config_url"])
    )
    if sso is None or cached is None:
        return None
    oauth_doc = cached[1]
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg not in _ALLOWED_JWT_ALGORITHMS:
            return None
        claims = jwt.decode(
            token, jwks.signing_key(oauth_doc["jwks_uri"], token), algorithms=[alg],
            audience=values["oauth_audience"], leeway=_JWT_LEEWAY_S,
            options={"require": ["iss", "sub", "exp"]},
        )
    except jwt.PyJWTError as exc:
        logger.debug("provider access token not accepted: %s", exc)
        return None
    iss, sub, tid = claims["iss"], claims["sub"], claims.get("tid")
    tid = tid if isinstance(tid, str) else ""
    if not isinstance(iss, str) or not isinstance(sub, str) or not sub:
        return None
    concrete = _concrete_issuer(sso["issuer"], iss, tid)
    if concrete is None or _concrete_issuer(oauth_doc["issuer"], iss, tid) is None:
        return None
    if _TENANT in sso["issuer"] and tid.lower() not in values["oidc_allowed_tenants"]:
        return None
    user = _holder(session, concrete, sub)
    return user if user is not None and user.active else None
