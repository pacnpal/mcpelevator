"""A fake OpenID Connect identity provider for SSO tests, served through an
``httpx.MockTransport`` installed as ``app.auth.oidc.TRANSPORT``.

It serves discovery, a JWKS from a generated RSA key, a token endpoint that mints ID
tokens with chosen claims (checking the PKCE verifier), and userinfo. The browser leg
(``/authorize``) is never fetched by the app: ``authorize`` plays the person consenting
and returns the code the provider would send back.

Knobs for later tests: ``claims`` (merged into every ID token), ``userinfo`` (claims
served there, ``None`` = no endpoint), ``token_error`` / ``token_status`` (the token
endpoint answers with an error), ``down`` (every endpoint 503s), ``id_token_override``
(hand back this exact ID token), ``sign(..., alg=, key=)`` and ``rotate_key()``.

Provider quirks, one setting each: ``alg`` (``RS256``, ``EdDSA``, or ``HS256`` signed with
``client_secret``), ``discovery_issuer`` (the issuer discovery advertises when it differs
from the ID token's ``iss``, e.g. Entra's ``{tenantid}`` template), ``jwks_issuer`` (an
``issuer`` field on every JWKS key, as Entra serves), ``client_secret`` (None = a public
client: the token endpoint wants ``client_id`` in the body and no client authentication)
and ``auth_methods`` (the advertised ``token_endpoint_auth_methods_supported``, enforced),
``end_session`` (discovery advertises an ``end_session_endpoint``).
``last_authorize`` holds the last authorization request's query; ``seen`` the path of every
request the app made to the provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from jwt.algorithms import OKPAlgorithm, RSAAlgorithm

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
        self.id_token_override: str | None = None
        self.iss_param = True  # send RFC 9207 ``iss`` on the callback
        self.alg = "RS256"
        self.discovery_issuer: str | None = None
        self.jwks_issuer: str | None = None
        self.client_secret: str | None = None
        self.auth_methods: list[str] | None = None
        self.end_session = False
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
        ]

    def sign(self, claims: dict[str, Any], *, alg: str | None = None, key: Any = None,
             kid: str | None = None) -> str:
        alg = alg or self.alg
        if alg == "HS256" and key is None:
            return jwt.encode(claims, self.client_secret, algorithm=alg)
        current_kid, private = self._keys[1 if alg == "EdDSA" else 0]
        return jwt.encode(claims, key if key is not None else private, algorithm=alg,
                          headers={"kid": kid or current_kid})

    def id_token(self, nonce: str, **extra: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer, "aud": self.client_id, "sub": "sub-1", "iat": now,
            "exp": now + 300, "nonce": nonce, **self.claims, **extra,
        }
        return self.sign(claims)

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
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            doc = {
                "issuer": self.discovery_issuer or self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks",
            }
            if self.userinfo is not None:
                doc["userinfo_endpoint"] = f"{self.issuer}/userinfo"
            if self.auth_methods is not None:
                doc["token_endpoint_auth_methods_supported"] = self.auth_methods
            if self.end_session:
                doc["end_session_endpoint"] = f"{self.issuer}/logout"
            return httpx.Response(200, json=doc)
        if path == "/jwks":
            keys = []
            for kid, private in self._keys:
                if isinstance(private, rsa.RSAPrivateKey):
                    jwk, alg = json.loads(RSAAlgorithm.to_jwk(private.public_key())), "RS256"
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
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_error:
            return httpx.Response(self.token_status, json={"error": self.token_error})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if not self._client_authenticated(request, form):
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = self._codes.pop(form.get("code", ""), None)
        if grant is None or form.get("redirect_uri") != grant["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant["code_challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        id_token = self.id_token_override or self.id_token(grant["nonce"])
        return httpx.Response(
            200, json={"access_token": "at-" + secrets.token_hex(8), "token_type": "Bearer",
                       "id_token": id_token, "expires_in": 300},
        )

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
