"""A fake OpenID Connect identity provider for SSO tests, served through an
``httpx.MockTransport`` installed as ``app.auth.oidc.TRANSPORT``.

It serves discovery, a JWKS from a generated RSA key, a token endpoint that mints ID
tokens with chosen claims (checking the PKCE verifier), and userinfo. The browser leg
(``/authorize``) is never fetched by the app: ``authorize`` plays the person consenting
and returns the code the provider would send back.

Knobs for later tests: ``claims`` (merged into every ID token), ``userinfo`` (claims
served there, ``None`` = no endpoint), ``token_error`` / ``token_status`` (the token
endpoint answers with an error), ``down`` (every endpoint 503s), ``jwks_down`` (the JWKS
alone 503s), ``userinfo_down`` (userinfo alone 503s),``id_token_override``
(hand back this exact ID token), ``token_barrier`` (a ``threading.Barrier`` every token
request waits at, so concurrent sign-ins reach the provider together), ``sign(..., alg=,
key=)`` and ``rotate_key()``.

Provider quirks, one setting each: ``alg`` (``RS256``, ``ES256``, ``EdDSA``, or ``HS256``
signed with ``client_secret``), ``discovery_issuer`` (the issuer discovery advertises when it differs
from the ID token's ``iss``, e.g. Entra's ``{tenantid}`` template), ``jwks_issuer`` (an
``issuer`` field on every JWKS key, as Entra serves), ``client_secret`` (None = a public
client: the token endpoint wants ``client_id`` in the body and no client authentication)
``auth_methods`` (the advertised ``token_endpoint_auth_methods_supported``, enforced),
``scopes_supported`` (advertised in discovery when set), ``end_session`` (discovery advertises an ``end_session_endpoint``)
and ``scope_claims`` / ``scope_userinfo`` (``{scope: claims}`` added to the ID token or to
userinfo only when the sign-in requested that scope). An issuer with a path (Authentik's
``/application/o/<slug>/``, Keycloak's ``/realms/<realm>``) serves every endpoint under it.

Refresh: every token response carries a single-use ``refresh_token`` (``refresh_tokens``
False: none) and ``expires_in`` (None: left out). A ``refresh_token`` grant spends the token
it presents (a reused one gets ``invalid_grant``) and answers with a new one and a new ID
token from the current ``claims`` (``refresh_id_token`` False: none). ``refresh_requires``
names a scope (or ``access_type=offline``, Google's way) the sign-in must have requested
for a refresh token at all; ``rotate_refresh`` False keeps the presented token valid and
answers a refresh with no new one. ``refreshes`` lists the refresh tokens the app
presented, in order.

``PRESETS`` describes each named provider as data, and ``FakeIdP.use(preset)`` plays it.
``last_authorize`` holds the last authorization request's query; ``seen`` the path of every
request the app made to the provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

ISSUER = "https://idp.example.com"
CLIENT_ID = "mcpelevator"


class FakeIdP:
    def __init__(self, issuer: str = ISSUER, client_id: str = CLIENT_ID) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.claims: dict[str, Any] = {}
        self.userinfo: dict[str, Any] | None = None
        self.token_error: str | None = None
        self.token_status = 400
        self.down = False
        self.jwks_down = False
        self.userinfo_down = False
        self.id_token_override: str | None = None
        self.token_barrier: Any = None
        self.iss_param = True  # send RFC 9207 ``iss`` on the callback
        self.alg = "RS256"
        self.discovery_issuer: str | None = None
        self.jwks_issuer: str | None = None
        self.client_secret: str | None = None
        self.auth_methods: list[str] | None = None
        self.scopes_supported: list[str] | None = None
        self.end_session = False
        self.scope_claims: dict[str, dict[str, Any]] = {}
        self.scope_userinfo: dict[str, dict[str, Any]] = {}
        self.refresh_requires: str | None = None
        self.rotate_refresh = True
        self._access: dict[str, dict[str, Any]] = {}
        self.refresh_tokens = True
        self.refresh_id_token = True
        self.expires_in: int | None = 300
        self.refreshes: list[str] = []
        self._refresh: dict[str, dict[str, Any]] = {}
        self.last_authorize: dict[str, str] = {}
        self.seen: list[str] = []
        self._codes: dict[str, dict[str, Any]] = {}
        self._keys: list[tuple[str, Any]] = []
        self.rotate_key()
        self.transport = httpx.MockTransport(self._handle)

    # ---- keys and tokens ----------------------------------------------------

    def rotate_key(self) -> None:
        """Publish a fresh signing key; the old one leaves the JWKS."""
        self._keys = [
            (secrets.token_hex(4), rsa.generate_private_key(public_exponent=65537, key_size=2048)),
            (secrets.token_hex(4), ed25519.Ed25519PrivateKey.generate()),
            (secrets.token_hex(4), ec.generate_private_key(ec.SECP256R1())),
        ]

    def sign(self, claims: dict[str, Any], *, alg: str | None = None, key: Any = None,
             kid: str | None = None) -> str:
        alg = alg or self.alg
        if alg == "HS256" and key is None:
            return jwt.encode(claims, self.client_secret, algorithm=alg)
        current_kid, private = self._keys[{"EdDSA": 1, "ES256": 2}.get(alg, 0)]
        return jwt.encode(claims, key if key is not None else private, algorithm=alg,
                          headers={"kid": kid or current_kid})

    def id_token(self, nonce: str, **extra: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer, "aud": self.client_id, "sub": "sub-1", "iat": now,
            "exp": now + 300, "nonce": nonce, **self.claims, **extra,
        }
        return self.sign(claims)

    def use(self, preset: Preset) -> None:
        """Behave as ``preset``'s provider."""
        self.issuer = preset.issuer
        self.discovery_issuer = preset.discovery_issuer
        self.jwks_issuer = preset.jwks_issuer
        self.alg = preset.alg
        self.client_secret = None if preset.public else "s3cret/+&="
        self.auth_methods = preset.auth_methods
        self.claims = dict(preset.id_token)
        self.userinfo = None if preset.userinfo is None else {"sub": "sub-1", **preset.userinfo}
        self.scope_claims = dict(preset.scope_claims)
        self.scope_userinfo = dict(preset.scope_userinfo)
        self.scopes_supported = preset.scopes_supported
        self.end_session = preset.end_session
        self.refresh_tokens = preset.refresh != "none"
        self.refresh_requires = None if preset.refresh in ("always", "none") else preset.refresh
        self.rotate_refresh = preset.rotating

    @staticmethod
    def _scoped(grant: dict[str, Any], by_scope: dict[str, dict[str, Any]]) -> dict[str, Any]:
        scopes = grant.get("scope", "").split()
        return {k: v for scope, claims in by_scope.items() if scope in scopes
                for k, v in claims.items()}

    # ---- the browser leg ----------------------------------------------------

    def authorize(self, location: str) -> dict[str, str]:
        """Consent to the authorization request at ``location``; returns the callback
        query the provider would redirect the browser back with."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == self.client_id
        assert q["code_challenge_method"] == "S256"
        self.last_authorize = q
        code = secrets.token_urlsafe(16)
        self._codes[code] = q
        out = {"code": code, "state": q["state"]}
        if self.iss_param:
            out["iss"] = self.claims.get("iss", self.issuer)
        return out

    # ---- the back channel ---------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.url.path)
        if self.down:
            return httpx.Response(503)
        base = self.issuer.rstrip("/")
        prefix = urlsplit(base).path
        path = request.url.path
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix):]
        if path == "/.well-known/openid-configuration":
            doc = {
                "issuer": self.discovery_issuer or self.issuer,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/jwks",
            }
            if self.userinfo is not None:
                doc["userinfo_endpoint"] = f"{base}/userinfo"
            if self.auth_methods is not None:
                doc["token_endpoint_auth_methods_supported"] = self.auth_methods
            if self.scopes_supported is not None:
                doc["scopes_supported"] = self.scopes_supported
            if self.end_session:
                doc["end_session_endpoint"] = f"{base}/logout"
            return httpx.Response(200, json=doc)
        if path == "/jwks":
            if self.jwks_down:
                return httpx.Response(503)
            keys = []
            for kid, private in self._keys:
                if isinstance(private, rsa.RSAPrivateKey):
                    jwk, alg = json.loads(RSAAlgorithm.to_jwk(private.public_key())), "RS256"
                elif isinstance(private, ec.EllipticCurvePrivateKey):
                    jwk, alg = json.loads(ECAlgorithm.to_jwk(private.public_key())), "ES256"
                else:
                    jwk, alg = json.loads(OKPAlgorithm.to_jwk(private.public_key())), "EdDSA"
                jwk = {**jwk, "kid": kid, "use": "sig", "alg": alg}
                if self.jwks_issuer is not None:
                    jwk["issuer"] = self.jwks_issuer
                keys.append(jwk)
            return httpx.Response(200, json={"keys": keys})
        if path == "/token":
            return self._token(request)
        if path == "/userinfo" and self.userinfo is not None:
            if self.userinfo_down:
                return httpx.Response(503)
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            scoped = self._scoped(self._access.get(token, {}), self.scope_userinfo)
            return httpx.Response(200, json={**self.userinfo, **scoped})
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_barrier is not None:
            self.token_barrier.wait()
        if self.token_error:
            return httpx.Response(self.token_status, json={"error": self.token_error})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if not self._client_authenticated(request, form):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "refresh_token":
            self.refreshes.append(form.get("refresh_token", ""))
            presented = form.get("refresh_token", "")
            spend = self._refresh.pop if self.rotate_refresh else self._refresh.get
            grant = spend(presented, None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._tokens(grant, id_token=self.refresh_id_token, refresh=self.rotate_refresh)
        grant = self._codes.pop(form.get("code", ""), None)
        if grant is None or form.get("redirect_uri") != grant["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant["code_challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return self._tokens(grant, id_token=True)

    def _tokens(self, grant: dict[str, Any], *, id_token: bool,
                refresh: bool = True) -> httpx.Response:
        access = "at-" + secrets.token_hex(8)
        self._access[access] = grant
        body: dict[str, Any] = {"access_token": access, "token_type": "Bearer"}
        if id_token:
            body["id_token"] = self.id_token_override or self.id_token(
                grant["nonce"], **self._scoped(grant, self.scope_claims)
            )
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        requested = {*grant.get("scope", "").split(), f"access_type={grant.get('access_type')}"}
        if refresh and self.refresh_tokens and self.refresh_requires in (None, *requested):
            token = "rt-" + secrets.token_hex(8)
            self._refresh[token] = grant
            body["refresh_token"] = token
        return httpx.Response(200, json=body)

    def _client_authenticated(self, request: httpx.Request, form: dict[str, str]) -> bool:
        header = request.headers.get("authorization", "")
        if self.client_secret is None:  # a public client: PKCE alone, no client auth
            return (form.get("client_id") == self.client_id and not header
                    and "client_secret" not in form)
        methods = self.auth_methods or ["client_secret_basic"]
        if header:
            # RFC 6749 §2.3.1: each half is form-encoded before the base64 pair.
            scheme, _, encoded = header.partition(" ")
            user, _, password = base64.b64decode(encoded).decode().partition(":")
            return ("client_secret_basic" in methods and scheme == "Basic"
                    and (unquote_plus(user), unquote_plus(password))
                    == (self.client_id, self.client_secret))
        return ("client_secret_post" in methods and form.get("client_id") == self.client_id
                and form.get("client_secret") == self.client_secret)


# ---- provider presets (#284) -----------------------------------------------------

BOTH = ["client_secret_basic", "client_secret_post"]
STANDARD = ["openid", "email", "profile"]
TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
ENTRA_TEMPLATE = "https://login.microsoftonline.com/{tenantid}/v2.0"
ADMINS_GUID = "5b0f2f38-4a26-4f6e-9a3c-0d7e3c1b2a99"
_ADA = {"email": "ada@example.com", "email_verified": True}


@dataclass(frozen=True)
class Preset:
    """What one provider does, as README "Provider notes" describes it, and the settings
    those notes tell an admin to use (``settings``, written through PATCH /api/settings).

    ``refresh`` is "always" (every sign-in gets a refresh token), "none", or the scope
    (or ``access_type=offline``) a sign-in must request to get one; ``rotating`` says
    whether each refresh hands back a new single-use token. ``admin_group`` is the group
    ``settings`` maps to Admin, None for a provider that sends no groups."""

    issuer: str
    admin_group: str | None
    settings: dict[str, Any] = field(default_factory=dict)
    discovery_issuer: str | None = None
    jwks_issuer: str | None = None
    alg: str = "RS256"
    public: bool = False
    auth_methods: list[str] | None = field(default_factory=lambda: list(BOTH))
    id_token: dict[str, Any] = field(default_factory=dict)
    userinfo: dict[str, Any] | None = None
    scope_claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    scope_userinfo: dict[str, dict[str, Any]] = field(default_factory=dict)
    scopes_supported: list[str] = field(default_factory=lambda: list(STANDARD))
    end_session: bool = False
    refresh: str = "always"
    rotating: bool = True


PRESETS: dict[str, Preset] = {
    # Trailing-slash issuer; email_verified false by default (2025.10+), so match by
    # preferred_username; refresh tokens only for the offline_access scope mapping.
    "authentik": Preset(
        issuer="https://authentik.example.com/application/o/mcpelevator/",
        id_token={"preferred_username": "ada", "email": "ada@example.com",
                  "email_verified": False, "groups": ["admins"]},
        scopes_supported=[*STANDARD, "offline_access"], refresh="offline_access",
        end_session=True, admin_group="admins",
        settings={"oidc_identity_claim": "preferred_username", "oidc_admin_groups": ["admins"]},
    ),
    # A public client, ES256; every claim but sub comes from userinfo, groups only when
    # the groups scope is asked for; single-use refresh tokens; no RP-initiated logout.
    "authelia": Preset(
        issuer="https://auth.example.com", alg="ES256", public=True,
        auth_methods=[*BOTH, "none"], userinfo=dict(_ADA),
        scope_userinfo={"groups": {"groups": ["admins"]}},
        scopes_supported=[*STANDARD, "groups", "offline_access"], refresh="offline_access",
        admin_group="admins", settings={"oidc_admin_groups": ["admins"]},
    ),
    # A realm issuer, an EdDSA realm key, realm roles under a nested path.
    "keycloak": Preset(
        issuer="https://keycloak.example.com/realms/acme", alg="EdDSA",
        id_token={**_ADA, "realm_access": {"roles": ["admins", "offline_access"]}},
        scopes_supported=[*STANDARD, "offline_access"], end_session=True,
        admin_group="admins",
        settings={"oidc_groups_claim": "realm_access.roles", "oidc_admin_groups": ["admins"]},
    ),
    # One tenant's issuer; keys name the {tenantid} template; no email_verified at all,
    # so match by preferred_username; groups are object IDs.
    "entra_single_tenant": Preset(
        issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0", jwks_issuer=ENTRA_TEMPLATE,
        id_token={"preferred_username": "ada@example.com", "tid": TENANT,
                  "groups": [ADMINS_GUID]},
        scopes_supported=[*STANDARD, "offline_access"], refresh="offline_access",
        end_session=True, admin_group=ADMINS_GUID,
        settings={"oidc_identity_claim": "preferred_username",
                  "oidc_admin_groups": [ADMINS_GUID]},
    ),
    # /organizations advertises the {tenantid} template: a tenant list is required.
    # App roles arrive in roles.
    "entra_multi_tenant": Preset(
        issuer="https://login.microsoftonline.com/organizations/v2.0",
        discovery_issuer=ENTRA_TEMPLATE, jwks_issuer=ENTRA_TEMPLATE,
        id_token={"iss": ENTRA_TEMPLATE.replace("{tenantid}", TENANT), "tid": TENANT,
                  "preferred_username": "ada@example.com", "roles": ["Admin"]},
        scopes_supported=[*STANDARD, "offline_access"], refresh="offline_access",
        end_session=True, admin_group="Admin",
        settings={"oidc_identity_claim": "preferred_username",
                  "oidc_allowed_tenants": [TENANT], "oidc_groups_claim": "roles",
                  "oidc_admin_groups": ["Admin"]},
    ),
    # A scheme-less iss, no groups (hd instead), refresh tokens for access_type=offline,
    # never rotated; no end_session_endpoint.
    "google": Preset(
        issuer="https://accounts.google.com",
        id_token={**_ADA, "iss": "accounts.google.com", "hd": "example.com"},
        refresh="access_type=offline", rotating=False, admin_group=None,
        settings={"oidc_allowed_domains": ["example.com"]},
    ),
    "okta_org": Preset(
        issuer="https://acme.okta.com", id_token={**_ADA, "groups": ["admins"]},
        auth_methods=[*BOTH, "client_secret_jwt", "private_key_jwt", "none"],
        scopes_supported=[*STANDARD, "groups", "offline_access"], refresh="offline_access",
        rotating=False, end_session=True, admin_group="admins",
        settings={"oidc_admin_groups": ["admins"]},
    ),
    "okta_custom": Preset(
        issuer="https://acme.okta.com/oauth2/default", id_token={**_ADA, "groups": ["admins"]},
        auth_methods=[*BOTH, "client_secret_jwt", "private_key_jwt", "none"],
        scopes_supported=[*STANDARD, "offline_access"], refresh="offline_access",
        end_session=True, admin_group="admins", settings={"oidc_admin_groups": ["admins"]},
    ),
    # Trailing-slash issuer; groups only from a post-login Action, under a URL name.
    "auth0": Preset(
        issuer="https://acme.auth0.com/",
        id_token={**_ADA, "https://example.com/groups": ["admins"]},
        auth_methods=[*BOTH, "private_key_jwt"],
        scopes_supported=[*STANDARD, "offline_access"], refresh="offline_access",
        rotating=False, end_session=True, admin_group="admins",
        settings={"oidc_groups_claim": "https://example.com/groups",
                  "oidc_admin_groups": ["admins"]},
    ),
    # EdDSA; claims from userinfo; project roles an object, only for the roles scope.
    "zitadel": Preset(
        issuer="https://acme.zitadel.cloud", alg="EdDSA",
        auth_methods=["none", *BOTH, "private_key_jwt"], userinfo=dict(_ADA),
        scope_userinfo={"urn:zitadel:iam:org:project:roles": {
            "urn:zitadel:iam:org:project:roles": {"admins": {"301": "acme.zitadel.cloud"}},
        }},
        scopes_supported=[*STANDARD, "offline_access"], refresh="offline_access",
        end_session=True, admin_group="admins",
        settings={"oidc_scopes": ["urn:zitadel:iam:org:project:roles"],
                  "oidc_groups_claim": "urn:zitadel:iam:org:project:roles",
                  "oidc_admin_groups": ["admins"]},
    ),
    # A per-client issuer, ES256 by default, basic auth; groups as SPN and UUID.
    "kanidm": Preset(
        issuer="https://idm.example.com/oauth2/openid/mcpelevator", alg="ES256",
        auth_methods=["client_secret_basic"], id_token=dict(_ADA),
        scope_claims={"groups": {"groups": [
            "admins@idm.example.com", "00000000-0000-4000-8000-00000000a0a0",
        ]}},
        scopes_supported=[*STANDARD, "groups"], admin_group="admins@idm.example.com",
        settings={"oidc_admin_groups": ["admins@idm.example.com"]},
    ),
    "pocket_id": Preset(
        issuer="https://id.example.com", alg="EdDSA", public=True,
        auth_methods=[*BOTH, "none"], id_token=dict(_ADA),
        scope_claims={"groups": {"groups": ["admins"]}},
        scopes_supported=[*STANDARD, "groups"], end_session=True,
        admin_group="admins", settings={"oidc_admin_groups": ["admins"]},
    ),
    # Full group paths only in userinfo; the ID token's groups_direct is direct ones.
    "gitlab": Preset(
        issuer="https://gitlab.com", id_token={**_ADA, "groups_direct": ["acme"]},
        userinfo={**_ADA, "groups": ["acme", "acme/admins"]},
        admin_group="acme/admins", settings={"oidc_admin_groups": ["acme/admins"]},
    ),
    # client_secret_post only; groups for the groups scope.
    "gitea_forgejo": Preset(
        issuer="https://git.example.com", auth_methods=["client_secret_post"],
        id_token=dict(_ADA), scope_claims={"groups": {"groups": ["admins"]}},
        scopes_supported=[*STANDARD, "groups"], admin_group="admins",
        settings={"oidc_admin_groups": ["admins"]},
    ),
    # A user-pool issuer; email_verified as a string; groups in cognito:groups;
    # refresh tokens never rotated; no end_session_endpoint.
    "aws_cognito": Preset(
        issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEf123",
        id_token={"email": "ada@example.com", "email_verified": "true",
                  "cognito:groups": ["admins"]},
        scopes_supported=["openid", "email", "phone", "profile"], rotating=False,
        admin_group="admins",
        settings={"oidc_groups_claim": "cognito:groups", "oidc_admin_groups": ["admins"]},
    ),
}
