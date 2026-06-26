"""Auth tests — token hashing, settings store, Host/Origin allowlist, resolution."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.auth import middleware
from app.db.models import Server
from app.registry import settings as runtime_settings
from app.util import hash_token, new_token


@pytest.fixture
def session():
    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_new_token_prefixed_unique_and_hash_stable():
    a, b = new_token(), new_token()
    assert a.startswith("mcpe_") and b.startswith("mcpe_")
    assert a != b
    assert hash_token(a) == hash_token(a)
    assert hash_token(a) != hash_token(b)


def test_settings_defaults_and_write(session):
    assert runtime_settings.read_all(session) == {
        "bind_mode": "local",
        "allowed_hosts": [],
        "default_auth_provider": "none",
    }
    runtime_settings.write(
        session,
        {"bind_mode": "expose", "allowed_hosts": ["mcp.example.com"], "default_auth_provider": "bearer"},
    )
    assert runtime_settings.bind_mode(session) == "expose"
    assert runtime_settings.allowed_hosts(session) == ["mcp.example.com"]
    assert runtime_settings.default_auth_provider(session) == "bearer"


def test_settings_write_rejects_bad_enums(session):
    with pytest.raises(ValueError):
        runtime_settings.write(session, {"bind_mode": "bogus"})
    with pytest.raises(ValueError):
        runtime_settings.write(session, {"default_auth_provider": "bogus"})
    # unknown keys are ignored; valid values still persist
    assert runtime_settings.write(session, {"nope": "x", "bind_mode": "expose"})["bind_mode"] == "expose"


def test_settings_write_normalizes_and_rejects_allowed_hosts(session):
    with pytest.raises(ValueError):  # malformed entry must not reach storage
        runtime_settings.write(session, {"allowed_hosts": ["[bad]"]})
    # a pasted URL / host:port is normalized down to its bare hostname
    result = runtime_settings.write(session, {"allowed_hosts": ["https://mcp.example.com:8080"]})
    assert result["allowed_hosts"] == ["mcp.example.com"]


def test_host_allowed_survives_malformed_stored_entry():
    # a malformed stored entry (legacy data) must be ignored, not crash the check
    ok, _ = middleware.host_allowed("mcp.example.com", None, ["[bad]", "mcp.example.com"])
    assert ok is True
    ok2, _ = middleware.host_allowed("evil.com", None, ["[bad]"])
    assert ok2 is False


@pytest.mark.parametrize(
    "host,origin,allowed,ok",
    [
        ("localhost:5173", None, [], True),  # loopback always allowed
        ("127.0.0.1:8080", None, [], True),
        ("[::1]:8080", None, [], True),  # ipv6 loopback
        ("mcp.example.com", None, ["mcp.example.com"], True),
        ("mcp.example.com:8080", None, ["mcp.example.com"], True),  # port stripped
        ("evil.com", None, ["mcp.example.com"], False),
        ("mcp.example.com", "https://evil.com", ["mcp.example.com"], False),  # bad origin
        ("mcp.example.com", "https://mcp.example.com", ["mcp.example.com"], True),
        ("", None, [], False),  # missing Host header must fail closed
        ("", None, ["mcp.example.com"], False),
        ("", "https://mcp.example.com", ["mcp.example.com"], False),  # good origin can't rescue a missing host
    ],
)
def test_host_allowed(host, origin, allowed, ok):
    result, _ = middleware.host_allowed(host, origin, allowed)
    assert result is ok


def _server(provider: str) -> Server:
    return Server(
        id="x", slug="x", name="x", runner="npx", command="npx", args=[], env={},
        auth_provider=provider,
    )


def test_resolve_provider():
    assert middleware.resolve(_server("none"), "bearer").name == "none"
    assert middleware.resolve(_server("bearer"), "none").name == "bearer"
    assert middleware.resolve(_server("inherit"), "bearer").name == "bearer"  # inherit -> default
    assert middleware.resolve(_server("inherit"), "none").name == "none"


def test_resolve_unknown_provider_fails_closed():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):  # unknown provider must not silently disable auth
        middleware.resolve(_server("bogus"), "none")
    with pytest.raises(HTTPException):  # inherit -> unknown default
        middleware.resolve(_server("inherit"), "bogus")
