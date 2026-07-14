"""``oauth`` auth-provider tests — settings validation, fail-closed, challenge
headers, JWT acceptance and the identity allowlist, and the RFC 9728 metadata route.

(Distinct from test_oauth.py, which covers UPSTREAM OAuth — mcpelevator as a client
to remote servers. This file covers mcpelevator as a RESOURCE SERVER for clients.)

No network: the verifier factory is monkeypatched to a JWTVerifier bound to a local
RSA keypair (fastmcp's test helper), and discovery is faked where needed.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
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

    async def fake_verifier_for(config_url: str, audience: str):
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
        with pytest.raises(ValueError):  # list of non-empty strings only
            runtime_settings.write(s, {"oauth_allowed_subjects": ["ok", ""]})
        with pytest.raises(ValueError):
            runtime_settings.write(s, {"oauth_allowed_subjects": "kyle"})
        out = runtime_settings.write(
            s,
            {
                "default_auth_provider": "oauth",  # now a valid enum member
                "oauth_config_url": f"{CONFIG_URL} ",  # trailing space is trimmed
                "oauth_allowed_subjects": ["Kyle", "kyle", "ada"],  # deduped, order kept
            },
        )
        assert out["default_auth_provider"] == "oauth"
        assert out["oauth_config_url"] == CONFIG_URL
        assert out["oauth_allowed_subjects"] == ["Kyle", "kyle", "ada"]


async def test_unconfigured_fails_closed(session):
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(_request(), _server())
    assert exc.value.status_code == 403


async def test_missing_token_401_with_resource_metadata(session, keypair):
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(_request(), _server())
    assert exc.value.status_code == 401
    challenge = exc.value.headers["WWW-Authenticate"]
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource/s/svc/mcp" in challenge


async def test_invalid_token_rejected(session, keypair):
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})
    req = _request({"Authorization": "Bearer not-a-jwt"})
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(req, _server())
    assert exc.value.status_code == 401
    assert 'error="invalid_token"' in exc.value.headers["WWW-Authenticate"]


async def test_wrong_issuer_rejected(session, keypair):
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})
    bad = _token(keypair, issuer="https://evil.example")
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": f"Bearer {bad}"}), _server()
        )
    assert exc.value.status_code == 401


async def test_valid_token_accepted(session, keypair):
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})
    tok = _token(keypair)
    # no exception = authorized
    await OAuthProvider().authenticate(
        _request({"Authorization": f"Bearer {tok}"}), _server()
    )


async def test_subject_allowlist(session, keypair):
    runtime_settings.write(
        session,
        {"oauth_config_url": CONFIG_URL, "oauth_allowed_subjects": ["Kyle"]},
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


async def test_as_outage_is_503_not_401(session, monkeypatch):
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})

    async def boom(config_url: str, audience: str):
        raise RuntimeError("jwks fetch failed")

    monkeypatch.setattr(oauth_mod, "_verifier_for", boom)
    with pytest.raises(HTTPException) as exc:
        await OAuthProvider().authenticate(
            _request({"Authorization": "Bearer whatever"}), _server()
        )
    assert exc.value.status_code == 503


async def test_metadata_route_advertises_issuer(session, monkeypatch):
    from app.auth.oauth import protected_resource_metadata

    session.add(_server())
    session.commit()
    runtime_settings.write(session, {"oauth_config_url": CONFIG_URL})

    async def fake_discovery(config_url: str):
        return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"}

    monkeypatch.setattr(oauth_mod, "_discovery", fake_discovery)
    out = await protected_resource_metadata("svc", _request({"Host": "box.local:8080"}))
    assert out["authorization_servers"] == [ISSUER]
    assert out["resource"].endswith("/s/svc/mcp")
    assert "scopes_supported" not in out  # empty setting -> field omitted

    runtime_settings.write(session, {"oauth_scopes": ["openid", "profile", "email"]})
    out = await protected_resource_metadata("svc", _request({"Host": "box.local:8080"}))
    assert out["scopes_supported"] == ["openid", "profile", "email"]


async def test_metadata_route_404_for_non_oauth_server(session):
    from app.auth.oauth import protected_resource_metadata

    with pytest.raises(HTTPException) as exc:
        await protected_resource_metadata("missing", _request())
    assert exc.value.status_code == 404


def test_config_url_normalization():
    assert oauth_mod._normalize_config_url("https://as.example") == (
        "https://as.example/.well-known/openid-configuration"
    )
    assert oauth_mod._normalize_config_url(CONFIG_URL) == CONFIG_URL
    rfc8414 = "https://as.example/.well-known/oauth-authorization-server"
    assert oauth_mod._normalize_config_url(rfc8414) == rfc8414


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
        session, {"oauth_config_url": CONFIG_URL, "oauth_accept_bearer": True}
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
