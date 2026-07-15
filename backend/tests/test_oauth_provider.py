"""``oauth`` auth-provider tests — settings validation, fail-closed, challenge
headers, JWT acceptance and the identity allowlist, and the RFC 9728 metadata route.

(Distinct from test_oauth.py, which covers UPSTREAM OAuth — mcpelevator as a client
to remote servers. This file covers mcpelevator as a RESOURCE SERVER for clients.)

No network: the verifier factory is monkeypatched to a JWTVerifier bound to a local
RSA keypair (fastmcp's test helper), and discovery is faked where needed.
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from joserfc import jwt
from joserfc.jwk import import_key
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app.auth import oauth as oauth_mod
from app.auth.oauth import OAuthProvider
from app.db.models import Server
from app.registry import settings as runtime_settings

ISSUER = "https://as.example"
AUDIENCE = "mcp"
CONFIG_URL = "https://as.example/.well-known/openid-configuration"


@pytest.fixture
def session(monkeypatch):
    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    # the provider opens its own Session(get_engine()) — point it at this store
    monkeypatch.setattr(oauth_mod, "get_engine", lambda: engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def keypair(monkeypatch):
    kp = RSAKeyPair.generate()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)

    async def fake_verifier_for(config_url: str, audience: str, algorithm: str):
        return verifier

    monkeypatch.setattr(oauth_mod, "_verifier_for", fake_verifier_for)
    return kp


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/s/svc/mcp",
        "headers": raw,
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 5555),
    }
    return Request(scope)


def _server() -> Server:
    return Server(id="sid", slug="svc", name="svc", auth_provider="oauth", args=[], env={})


def _token(kp: RSAKeyPair, **kwargs) -> str:
    defaults = dict(subject="user-1", issuer=ISSUER, audience=AUDIENCE)
    defaults.update(kwargs)
    return kp.create_token(**defaults)


def test_settings_validation():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        with pytest.raises(ValueError):  # not a URL
            runtime_settings.write(s, {"oauth_config_url": "as.example"})
        with pytest.raises(ValueError):  # scheme without a host
            runtime_settings.write(s, {"oauth_config_url": "https://"})
        with pytest.raises(ValueError):  # malformed port
            runtime_settings.write(s, {"oauth_config_url": "https://as.example:bad"})
        with pytest.raises(ValueError):  # cleartext is safe only for loopback dev
            runtime_settings.write(s, {"oauth_config_url": "http://as.example"})
        loopback = runtime_settings.write(
            s, {"oauth_config_url": "http://127.0.0.1:9000"}
        )
        assert loopback["oauth_config_url"] == "http://127.0.0.1:9000"
        with pytest.raises(ValueError):  # list of non-empty strings only
            runtime_settings.write(s, {"oauth_allowed_subjects": ["ok", ""]})
        with pytest.raises(ValueError):
            runtime_settings.write(s, {"oauth_allowed_subjects": "kyle"})
        out = runtime_settings.write(
            s,
            {
                "default_auth_provider": "oauth",  # now a valid enum member
                "oauth_config_url": f"{CONFIG_URL} ",  # trailing space is trimmed
                "oauth_audience": " mcp ",
                "oauth_allowed_subjects": ["Kyle", "kyle", "ada"],  # deduped, order kept
            },
        )
        assert out["default_auth_provider"] == "oauth"
        assert out["oauth_config_url"] == CONFIG_URL
        assert out["oauth_audience"] == AUDIENCE
        assert out["oauth_allowed_subjects"] == ["Kyle", "kyle", "ada"]


async def test_unconfigured_fails_closed(session):
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(_request(), _server())
    assert exc.value.status_code == 403


async def test_missing_token_401_with_resource_metadata(session, keypair):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(_request(), _server())
    assert exc.value.status_code == 401
    challenge = exc.value.headers["WWW-Authenticate"]
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource/s/svc/mcp" in challenge


async def test_challenge_honors_forwarded_https(session, keypair, monkeypatch):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    monkeypatch.setattr(oauth_mod, "base_url", lambda request: "http://mcp.example.com")
    monkeypatch.setattr(
        oauth_mod, "get_settings", lambda: SimpleNamespace(public_base_url=None)
    )

    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"X-Forwarded-Proto": "https"}), _server()
        )

    assert 'resource_metadata="https://mcp.example.com/' in (
        exc.value.headers["WWW-Authenticate"]
    )


async def test_invalid_token_rejected(session, keypair):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    req = _request({"Authorization": "Bearer not-a-jwt"})
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(req, _server())
    assert exc.value.status_code == 401
    assert 'error="invalid_token"' in exc.value.headers["WWW-Authenticate"]


async def test_wrong_issuer_rejected(session, keypair):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    bad = _token(keypair, issuer="https://evil.example")
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {bad}"}), _server()
        )
    assert exc.value.status_code == 401


async def test_wrong_audience_rejected(session, keypair):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    bad = _token(keypair, audience="another-api")
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {bad}"}), _server()
        )
    assert exc.value.status_code == 401


async def test_valid_token_accepted(session, keypair):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    tok = _token(keypair)
    # no exception = authorized
    await OAuthProvider().authenticate(
        _request({"Authorization": f"Bearer {tok}"}), _server()
    )


async def test_subject_allowlist(session, keypair):
    runtime_settings.write(
        session,
        {
            "oauth_config_url": CONFIG_URL,
            "oauth_audience": AUDIENCE,
            "oauth_allowed_subjects": ["Kyle"],
        },
    )
    ok = _token(keypair, subject="ignored", additional_claims={"preferred_username": "kyle"})
    await OAuthProvider().authenticate(
        _request({"Authorization": f"Bearer {ok}"}), _server()
    )
    outsider = _token(keypair, subject="someone-else")
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {outsider}"}), _server()
        )
    assert exc.value.status_code == 403


async def test_subject_allowlist_matches_sub_case_sensitively(session, keypair):
    runtime_settings.write(
        session,
        {
            "oauth_config_url": CONFIG_URL,
            "oauth_audience": AUDIENCE,
            "oauth_allowed_subjects": ["User-1"],
        },
    )
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {_token(keypair, subject='user-1')}"}),
            _server(),
        )
    assert exc.value.status_code == 403

    await OAuthProvider().authenticate(
        _request({"Authorization": f"Bearer {_token(keypair, subject='User-1')}"}),
        _server(),
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": None},
        {"nbf": int(time.time()) + 3600},
    ],
)
async def test_token_requires_current_bounded_lifetime(session, keypair, claims):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    token = _token(keypair, additional_claims=claims)
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {token}"}), _server()
        )
    assert exc.value.status_code == 401


async def test_as_outage_is_503_not_401(session, keypair, monkeypatch, caplog):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )

    async def boom(config_url: str, audience: str, algorithm: str):
        raise RuntimeError("jwks fetch failed")

    monkeypatch.setattr(oauth_mod, "_verifier_for", boom)
    caplog.set_level("ERROR", logger=oauth_mod.__name__)
    token = _token(keypair)
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {token}"}), _server()
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == "authorization server unavailable"
    assert "jwks fetch failed" in caplog.text


async def test_real_jwks_transport_failure_is_503(session, keypair, monkeypatch):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("jwks unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        verifier = oauth_mod._OAuthJWTVerifier(
            jwks_uri=f"{ISSUER}/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            http_client=client,
        )

        async def real_verifier(config_url: str, audience: str, algorithm: str):
            return verifier

        monkeypatch.setattr(oauth_mod, "_verifier_for", real_verifier)
        token = _token(keypair, kid="current")
        with pytest.raises(HTTPException) as exc:
            await OAuthProvider().authenticate(
                _request({"Authorization": f"Bearer {token}"}), _server()
            )
    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    "jwks",
    [
        [],
        {"keys": []},
        {"keys": [{"kty": "RSA", "kid": "broken", "alg": "RS256"}]},
    ],
)
async def test_unusable_jwks_is_503(session, keypair, monkeypatch, jwks):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=jwks))
    ) as client:
        verifier = oauth_mod._OAuthJWTVerifier(
            jwks_uri=f"{ISSUER}/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            http_client=client,
        )

        async def real_verifier(config_url: str, audience: str, algorithm: str):
            return verifier

        monkeypatch.setattr(oauth_mod, "_verifier_for", real_verifier)
        token = _token(keypair, kid="current")
        with pytest.raises(HTTPException) as exc:
            await OAuthProvider().authenticate(
                _request({"Authorization": f"Bearer {token}"}), _server()
            )
    assert exc.value.status_code == 503


async def test_jwks_ignores_unrelated_unusable_keys(session, keypair, monkeypatch):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    valid = {
        **import_key(keypair.public_key, "RSA").as_dict(),
        "kid": "known",
        "alg": "RS256",
    }
    jwks = {"keys": [{"kty": "OKP", "kid": "unrelated"}, valid]}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=jwks)
        )
    ) as client:
        verifier = oauth_mod._OAuthJWTVerifier(
            jwks_uri=f"{ISSUER}/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            http_client=client,
        )

        async def real_verifier(config_url: str, audience: str, algorithm: str):
            return verifier

        monkeypatch.setattr(oauth_mod, "_verifier_for", real_verifier)
        token = _token(keypair, kid="known")
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {token}"}), _server()
        )


async def test_unknown_kid_on_usable_jwks_is_401(session, keypair, monkeypatch):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    jwk = {
        **import_key(keypair.public_key, "RSA").as_dict(),
        "kid": "known",
        "alg": "RS256",
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [jwk]})
        )
    ) as client:
        verifier = oauth_mod._OAuthJWTVerifier(
            jwks_uri=f"{ISSUER}/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            http_client=client,
        )

        async def real_verifier(config_url: str, audience: str, algorithm: str):
            return verifier

        monkeypatch.setattr(oauth_mod, "_verifier_for", real_verifier)
        token = _token(keypair, kid="missing")
        with pytest.raises(HTTPException) as exc:
            await OAuthProvider().authenticate(
                _request({"Authorization": f"Bearer {token}"}), _server()
            )
    assert exc.value.status_code == 401


async def test_unknown_kids_share_a_jwks_refresh_cooldown(session, keypair, monkeypatch):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    requests = 0
    jwk = {
        **import_key(keypair.public_key, "RSA").as_dict(),
        "kid": "known",
        "alg": "RS256",
    }

    def jwks(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(jwks)) as client:
        verifier = oauth_mod._OAuthJWTVerifier(
            jwks_uri=f"{ISSUER}/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            http_client=client,
        )

        async def real_verifier(config_url: str, audience: str, algorithm: str):
            return verifier

        monkeypatch.setattr(oauth_mod, "_verifier_for", real_verifier)
        for kid in ("missing-1", "missing-2", "missing-3"):
            token = _token(keypair, kid=kid)
            with pytest.raises(HTTPException) as exc:
                await OAuthProvider().authenticate(
                    _request({"Authorization": f"Bearer {token}"}), _server()
                )
            assert exc.value.status_code == 401

    assert requests == 1


async def test_es256_token_is_verified_end_to_end(session, monkeypatch):
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = jwt.encode(
        {"alg": "ES256", "kid": "ec-key"},
        {
            "sub": "user-1",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        import_key(private_pem, "EC"),
        algorithms=["ES256"],
    )
    jwk = {
        **import_key(public_pem, "EC").as_dict(),
        "kid": "ec-key",
        "alg": "ES256",
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [jwk]})
        )
    ) as client:
        verifier = oauth_mod._OAuthJWTVerifier(
            jwks_uri=f"{ISSUER}/jwks",
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithm="ES256",
            http_client=client,
        )

        async def real_verifier(config_url: str, audience: str, algorithm: str):
            assert algorithm == "ES256"
            return verifier

        monkeypatch.setattr(oauth_mod, "_verifier_for", real_verifier)
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {token}"}), _server()
        )


async def test_metadata_route_advertises_issuer(session, monkeypatch):
    from app.auth.oauth import protected_resource_metadata

    session.add(_server())
    session.commit()
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )

    async def fake_discovery(config_url: str):
        return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"}

    monkeypatch.setattr(oauth_mod, "_discovery", fake_discovery)
    monkeypatch.setattr(oauth_mod, "base_url", lambda request: "http://mcp.example.com")
    monkeypatch.setattr(
        oauth_mod, "get_settings", lambda: SimpleNamespace(public_base_url=None)
    )
    out = await protected_resource_metadata(
        "s",
        "svc",
        _request({"Host": "127.0.0.1:8080", "X-Forwarded-Proto": "https"}),
    )
    assert out["authorization_servers"] == [ISSUER]
    assert out["resource"] == "https://mcp.example.com/s/svc/mcp"
    assert "scopes_supported" not in out  # empty setting -> field omitted

    runtime_settings.write(session, {"oauth_scopes": ["openid", "profile", "email"]})
    out = await protected_resource_metadata(
        "s",
        "svc",
        _request({"Host": "127.0.0.1:8080", "X-Forwarded-Proto": "https"}),
    )
    assert out["scopes_supported"] == ["openid", "profile", "email"]


async def test_metadata_route_supports_oauth_groups(session, monkeypatch):
    from app.auth.oauth import protected_resource_metadata

    runtime_settings.write(
        session,
        {
            "groups": {"team": []},
            "default_auth_provider": "oauth",
            "oauth_config_url": CONFIG_URL,
            "oauth_audience": AUDIENCE,
        },
    )

    async def fake_discovery(config_url: str):
        return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"}

    monkeypatch.setattr(oauth_mod, "_discovery", fake_discovery)
    out = await protected_resource_metadata(
        "g", "team", _request({"Host": "127.0.0.1:8080"})
    )
    assert out["resource"].endswith("/g/team/mcp")


async def test_metadata_route_rejects_off_allowlist_host(session):
    from app.auth.oauth import protected_resource_metadata

    session.add(_server())
    session.commit()
    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )

    with pytest.raises(HTTPException) as exc:
        await protected_resource_metadata(
            "s", "svc", _request({"Host": "evil.example"})
        )
    assert exc.value.status_code == 403


async def test_group_challenge_points_to_group_metadata(session):
    from app.groups.hub import group_server

    runtime_settings.write(
        session, {"oauth_config_url": CONFIG_URL, "oauth_audience": AUDIENCE}
    )
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(_request(), group_server("team"))
    assert exc.value.status_code == 401
    assert "/.well-known/oauth-protected-resource/g/team/mcp" in (
        exc.value.headers["WWW-Authenticate"]
    )


async def test_metadata_route_404_for_non_oauth_server(session):
    from app.auth.oauth import protected_resource_metadata

    with pytest.raises(HTTPException) as exc:
        await protected_resource_metadata(
            "s", "missing", _request({"Host": "127.0.0.1"})
        )
    assert exc.value.status_code == 404


def test_config_url_normalization():
    assert oauth_mod._normalize_config_url("https://as.example") == (
        "https://as.example/.well-known/openid-configuration"
    )
    assert oauth_mod._normalize_config_url(CONFIG_URL) == CONFIG_URL
    rfc8414 = "https://as.example/.well-known/oauth-authorization-server"
    assert oauth_mod._normalize_config_url(rfc8414) == rfc8414
    assert oauth_mod._normalize_config_url("https://well-known.example") == (
        "https://well-known.example/.well-known/openid-configuration"
    )


def test_oauth_endpoint_urls_require_https_outside_loopback():
    assert runtime_settings.is_valid_oauth_endpoint_url("https://as.example/jwks")
    assert runtime_settings.is_valid_oauth_endpoint_url("http://localhost:9000/jwks")
    assert not runtime_settings.is_valid_oauth_endpoint_url("http://as.example/jwks")
    assert not runtime_settings.is_valid_oauth_endpoint_url(
        "https://user:secret@as.example/jwks"
    )


def test_jwt_algorithm_accepts_only_asymmetric_allowlist():
    def token_for(algorithm: object) -> str:
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": algorithm}).encode()
        ).rstrip(b"=").decode()
        return f"{header}.payload.signature"

    for algorithm in ("RS256", "PS256", "ES256"):
        assert oauth_mod._jwt_algorithm(token_for(algorithm)) == algorithm
    for algorithm in ("HS256", "none", "bogus"):
        assert oauth_mod._jwt_algorithm(token_for(algorithm)) is None
    assert oauth_mod._jwt_algorithm(token_for([])) is None


async def test_missing_audience_fails_closed(session, keypair):
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})
    token = _token(keypair)
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {token}"}), _server()
        )
    assert exc.value.status_code == 403


async def test_accept_bearer_delegates_local_tokens(session, keypair, monkeypatch):
    """With oauth_accept_bearer on, an mcpe_ token gets the bearer provider's verdict
    (accept + scope semantics); JWTs still go down the OAuth path; and with the
    setting off (default) an mcpe_ token is treated as an (invalid) JWT."""
    from app.auth import bearer as bearer_mod
    from app.db import repo
    from app.db.models import Token
    from app.util import hash_token

    monkeypatch.setattr(bearer_mod, "get_engine", oauth_mod.get_engine)
    runtime_settings.write(
        session,
        {
            "oauth_config_url": CONFIG_URL,
            "oauth_audience": AUDIENCE,
            "oauth_accept_bearer": True,
        },
    )
    session.add(
        Token(id="t1", name="auto", token_hash=hash_token("mcpe_localtoken"), prefix="mcpe_loc", scope="all")
    )
    session.commit()

    req = _request({"Authorization": "Bearer mcpe_localtoken"})
    await OAuthProvider().authenticate(req, _server())  # local token accepted

    scoped = Token(
        id="t2", name="other", token_hash=hash_token("mcpe_scoped"), prefix="mcpe_sco", scope="other-id"
    )
    session.add(scoped)
    session.commit()
    with pytest.raises(HTTPException) as exc:  # bearer 403 scope semantics preserved
        await OAuthProvider().authenticate(
            _request({"Authorization": "Bearer mcpe_scoped"}), _server()
        )
    assert exc.value.status_code == 403

    jwt_tok = _token(keypair)  # JWTs still verify on the same endpoint
    await OAuthProvider().authenticate(
        _request({"Authorization": f"Bearer {jwt_tok}"}), _server()
    )

    runtime_settings.write(session, {"oauth_accept_bearer": False})
    with pytest.raises(HTTPException) as exc:  # default-off: mcpe_ token is a bad JWT
        await OAuthProvider().authenticate(
            _request({"Authorization": "Bearer mcpe_localtoken"}), _server()
        )
    assert exc.value.status_code == 401
