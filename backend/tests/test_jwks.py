"""The shared signing-key cache, driven directly with a fake key set."""

from types import SimpleNamespace

import jwt
import pytest

URL = "https://issuer.example/jwks"


def test_an_unknown_kid_refetches_once_per_window(monkeypatch):
    from app.auth import jwks

    fetches = []

    def get_signing_keys(refresh: bool = False):
        fetches.append(refresh)
        keys = [SimpleNamespace(key_id="k1", key="pem-1")]
        return [*keys, SimpleNamespace(key_id="rotated", key="pem-2")] if refresh else keys

    monkeypatch.setattr(jwks, "_keys", lambda url: SimpleNamespace(
        get_signing_keys=get_signing_keys, jwk_set_cache=None))
    monkeypatch.setattr(jwks, "_last_refetch", {})
    monkeypatch.setattr(jwks, "_last_failure", {})
    monkeypatch.setattr(jwks, "_good", {})

    def token(kid: str) -> str:
        return jwt.encode({}, "secret", algorithm="HS256", headers={"kid": kid})

    assert jwks.signing_key(URL, token("k1")) == "pem-1"
    assert fetches.count(True) == 0
    # Rotation: a kid the cached set lacks refetches it once, and the new key answers.
    assert jwks.signing_key(URL, token("rotated")) == "pem-2"
    assert fetches.count(True) == 1
    # A second unknown kid inside the window is refused without another refetch.
    with pytest.raises(jwt.InvalidTokenError):
        jwks.signing_key(URL, token("junk"))
    assert fetches.count(True) == 1


def _serve(monkeypatch, *keys: tuple[str | None, str]) -> None:
    from app.auth import jwks

    monkeypatch.setattr(jwks, "_keys", lambda url: SimpleNamespace(
        get_signing_keys=lambda refresh=False: [SimpleNamespace(key_id=k, key=v) for k, v in keys],
        jwk_set_cache=None))
    monkeypatch.setattr(jwks, "_last_refetch", {})
    monkeypatch.setattr(jwks, "_last_failure", {})
    monkeypatch.setattr(jwks, "_good", {})


def _no_kid() -> str:
    return jwt.encode({}, "secret", algorithm="HS256")


def test_a_token_without_a_kid_uses_the_only_key(monkeypatch):
    from app.auth import jwks

    _serve(monkeypatch, ("k1", "pem-1"))
    assert jwks.signing_key(URL, _no_kid()) == "pem-1"


def test_a_token_without_a_kid_is_refused_when_several_keys_are_published(monkeypatch):
    from app.auth import jwks

    _serve(monkeypatch, ("k1", "pem-1"), ("k2", "pem-2"))
    with pytest.raises(jwt.InvalidTokenError):
        jwks.signing_key(URL, _no_kid())
