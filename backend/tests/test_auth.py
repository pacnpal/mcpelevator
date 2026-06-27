"""Auth tests — token hashing, settings store, Host/Origin allowlist, resolution."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app.auth import middleware
from app.auth.bearer import BearerProvider
from app.db import repo
from app.db.models import Server, Token
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token


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
        "control_plane_auth": "auto",
        "allow_private_lan": False,
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
    with pytest.raises(ValueError):  # allow_private_lan must be a bool, not a string
        runtime_settings.write(session, {"allow_private_lan": "yes"})
    # unknown keys are ignored; valid values still persist
    assert runtime_settings.write(session, {"nope": "x", "bind_mode": "expose"})["bind_mode"] == "expose"


def test_settings_write_normalizes_and_rejects_allowed_hosts(session):
    with pytest.raises(ValueError):  # malformed entry must not reach storage
        runtime_settings.write(session, {"allowed_hosts": ["[bad]"]})
    # a pasted URL / host:port is normalized down to its bare hostname
    result = runtime_settings.write(session, {"allowed_hosts": ["https://mcp.example.com:8080"]})
    assert result["allowed_hosts"] == ["mcp.example.com"]
    # IPv6 literals normalize to the bare address, from bracketed or bare input
    assert runtime_settings.write(session, {"allowed_hosts": ["[2001:db8::1]"]})["allowed_hosts"] == [
        "2001:db8::1"
    ]
    assert runtime_settings.write(session, {"allowed_hosts": ["2001:db8::1"]})["allowed_hosts"] == [
        "2001:db8::1"
    ]
    # duplicates after normalization (host vs host:port vs case) collapse, order kept
    assert runtime_settings.write(
        session, {"allowed_hosts": ["mcp.example.com", "mcp.example.com:8080", "MCP.example.com"]}
    )["allowed_hosts"] == ["mcp.example.com"]


def test_settings_write_is_atomic_on_invalid_patch(session):
    runtime_settings.write(session, {"bind_mode": "expose"})
    # a patch that would flip bind_mode but has a later invalid field must commit
    # nothing — otherwise a 400 response could still e.g. lock out the control plane.
    with pytest.raises(ValueError):
        runtime_settings.write(session, {"bind_mode": "local", "default_auth_provider": "bogus"})
    assert runtime_settings.bind_mode(session) == "expose"  # unchanged — atomic


def test_host_allowed_survives_malformed_stored_entry():
    # a malformed stored entry (legacy data) must be ignored, not crash the check
    ok, _ = middleware.host_allowed(
        "mcp.example.com", None, ["[bad]", "mcp.example.com"], client_is_loopback=False
    )
    assert ok is True
    ok2, _ = middleware.host_allowed("evil.com", None, ["[bad]"], client_is_loopback=False)
    assert ok2 is False


def test_request_allowlist_trusts_configured_public_host(session, monkeypatch):
    from types import SimpleNamespace

    # local mode + a configured public host -> the public host is allowed, so the
    # advertised public URL doesn't 403 itself before it can be allowlisted.
    monkeypatch.setattr(
        middleware, "get_settings", lambda: SimpleNamespace(public_host="mcp.example.com")
    )
    allowed = middleware.request_allowlist(session)
    assert "mcp.example.com" in allowed
    ok, _ = middleware.host_allowed("mcp.example.com", None, allowed, client_is_loopback=False)
    assert ok is True


def test_host_allowed_ipv6_literal():
    # IPv6 entries (stored bare after normalization, or bracketed) must match a
    # bracketed request Host — host_only brackets bare literals so they round-trip.
    assert (
        middleware.host_allowed("[2001:db8::1]:8080", None, ["2001:db8::1"], client_is_loopback=False)[0]
        is True
    )
    assert (
        middleware.host_allowed("[2001:db8::1]", None, ["[2001:db8::1]"], client_is_loopback=False)[0]
        is True
    )
    assert (
        middleware.host_allowed("[2001:db8::2]", None, ["2001:db8::1"], client_is_loopback=False)[0]
        is False
    )


@pytest.mark.parametrize(
    "host,origin,allowed,client_loopback,ok",
    [
        ("localhost:5173", None, [], True, True),  # loopback host from a loopback peer
        ("127.0.0.1:8080", None, [], True, True),
        ("[::1]:8080", None, [], True, True),  # ipv6 loopback
        # P1 fix: a loopback Host from a NON-loopback peer must not pass (an off-host
        # bind spoofing Host: localhost to skip the allowlist).
        ("localhost:5173", None, [], False, False),
        ("127.0.0.1:8080", None, [], False, False),
        ("mcp.example.com", None, ["mcp.example.com"], False, True),
        ("mcp.example.com:8080", None, ["mcp.example.com"], False, True),  # port stripped
        ("evil.com", None, ["mcp.example.com"], False, False),
        ("mcp.example.com", "https://evil.com", ["mcp.example.com"], False, False),  # bad origin
        ("mcp.example.com", "https://mcp.example.com", ["mcp.example.com"], False, True),
        ("mcp.example.com", "[bad", ["mcp.example.com"], False, False),  # malformed Origin fails closed
        ("", None, [], True, False),  # missing Host fails closed, even from loopback
        ("", None, ["mcp.example.com"], False, False),
        ("", "https://mcp.example.com", ["mcp.example.com"], False, False),  # good origin can't rescue a missing host
    ],
)
def test_host_allowed(host, origin, allowed, client_loopback, ok):
    result, _ = middleware.host_allowed(host, origin, allowed, client_is_loopback=client_loopback)
    assert result is ok


def test_host_allowed_private_lan_literal():
    # allow_private=True lets a private-IP literal Host pass without an allowlist entry
    # (the LAN-access path), but a HOSTNAME never does — that's what keeps it
    # rebinding-safe (a rebound attack sends a domain, not a private-IP literal).
    assert middleware.host_allowed(
        "192.168.1.50:8080", None, [], client_is_loopback=False, allow_private=True
    )[0] is True
    assert middleware.host_allowed(
        "10.0.0.5", None, [], client_is_loopback=False, allow_private=True
    )[0] is True
    assert middleware.host_allowed(
        "[fd00::1]", None, [], client_is_loopback=False, allow_private=True
    )[0] is True
    # a public IP literal is NOT private -> rejected even with allow_private
    assert middleware.host_allowed(
        "8.8.8.8", None, [], client_is_loopback=False, allow_private=True
    )[0] is False
    # a hostname that might resolve to a private IP is rejected (rebinding defense)
    assert middleware.host_allowed(
        "nas.local", None, [], client_is_loopback=False, allow_private=True
    )[0] is False
    # Origin is held to the same rule: a private-IP Host with an off-allowlist domain
    # Origin still fails closed
    assert middleware.host_allowed(
        "192.168.1.50", "https://evil.com", [], client_is_loopback=False, allow_private=True
    )[0] is False
    # without the flag, a private-IP literal is just another off-allowlist host
    assert middleware.host_allowed(
        "192.168.1.50", None, [], client_is_loopback=False, allow_private=False
    )[0] is False
    # loopback / unspecified literals do NOT pass via the LAN literal path — they're
    # honoured only through the peer-gated _LOOPBACK set, never spoofed from a LAN peer
    assert middleware.host_allowed(
        "127.0.0.1", None, [], client_is_loopback=False, allow_private=True
    )[0] is False
    assert middleware.host_allowed(
        "0.0.0.0", None, [], client_is_loopback=False, allow_private=True
    )[0] is False
    # but a real loopback peer still gets loopback Host via _LOOPBACK, LAN flag or not
    assert middleware.host_allowed(
        "127.0.0.1", None, [], client_is_loopback=True, allow_private=True
    )[0] is True


def test_settings_update_rejects_coercible_allow_private_lan():
    from pydantic import ValidationError

    from app.api.schemas import SettingsUpdate

    assert SettingsUpdate(allow_private_lan=True).allow_private_lan is True
    assert SettingsUpdate().allow_private_lan is None  # still optional
    with pytest.raises(ValidationError):  # StrictBool: no "yes"/"true"/1 coercion
        SettingsUpdate(allow_private_lan="yes")


def test_is_private_client():
    from types import SimpleNamespace

    def req(peer):
        client = SimpleNamespace(host=peer) if peer is not None else None
        return SimpleNamespace(client=client)

    assert middleware.is_private_client(req("127.0.0.1")) is True  # loopback qualifies
    assert middleware.is_private_client(req("192.168.1.50")) is True  # RFC 1918
    assert middleware.is_private_client(req("10.0.0.5")) is True
    assert middleware.is_private_client(req("172.16.4.4")) is True
    assert middleware.is_private_client(req("fd00::1")) is True  # IPv6 ULA
    assert middleware.is_private_client(req("fe80::1%eth0")) is True  # link-local + zone id
    assert middleware.is_private_client(req("8.8.8.8")) is False  # public
    assert middleware.is_private_client(req("not-an-ip")) is False  # unparseable peer
    assert middleware.is_private_client(req(None)) is False


def test_private_lan_allowed_requires_setting_and_private_peer(session, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        middleware, "get_settings", lambda: SimpleNamespace(trusted_proxies="")
    )

    def req(peer):
        return SimpleNamespace(client=SimpleNamespace(host=peer))

    # setting off -> never allowed, even from a LAN peer
    assert middleware.private_lan_allowed(req("192.168.1.50"), session) is False
    runtime_settings.write(session, {"allow_private_lan": True})
    # setting on + private peer -> allowed
    assert middleware.private_lan_allowed(req("192.168.1.50"), session) is True
    # setting on + public peer -> still not allowed (the peer gate)
    assert middleware.private_lan_allowed(req("8.8.8.8"), session) is False


def test_is_loopback_client():
    from types import SimpleNamespace

    def req(peer):
        client = SimpleNamespace(host=peer) if peer is not None else None
        return SimpleNamespace(client=client)

    assert middleware.is_loopback_client(req("127.0.0.1")) is True
    assert middleware.is_loopback_client(req("::1")) is True
    assert middleware.is_loopback_client(req("testclient")) is True  # starlette TestClient peer
    assert middleware.is_loopback_client(req("10.0.0.5")) is False  # LAN
    assert middleware.is_loopback_client(req("203.0.113.7")) is False  # public
    assert middleware.is_loopback_client(req(None)) is False  # no client info


def test_is_loopback_client_trusts_configured_proxy(monkeypatch):
    from types import SimpleNamespace

    def req(peer):
        return SimpleNamespace(client=SimpleNamespace(host=peer))

    # Without a trusted-proxy config, the compose gateway peer is NOT loopback.
    assert middleware.is_loopback_client(req("172.20.0.1")) is False
    # With MCPE_TRUSTED_PROXIES = the gateway /32 (the compose default), only that
    # exact address is trusted — a sibling container on the same network is not.
    monkeypatch.setattr(
        middleware, "get_settings", lambda: SimpleNamespace(trusted_proxies="172.20.0.1/32")
    )
    assert middleware.is_loopback_client(req("172.20.0.1")) is True  # the gateway
    assert middleware.is_loopback_client(req("172.20.0.5")) is False  # a sibling container


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


# --- per-server token scope --------------------------------------------------


def _bearer_request(token: str | None) -> Request:
    """Minimal ASGI request carrying ``Authorization: Bearer <token>``."""
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "headers": headers})


@pytest.fixture
def bearer_engine(monkeypatch):
    """In-memory DB that ``BearerProvider`` reads from (it opens its own session
    via ``get_engine``). StaticPool keeps every connection on the one DB."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.auth.bearer.get_engine", lambda: engine)
    return engine


def _make_server(engine, server_id: str) -> Server:
    server = Server(
        id=server_id, slug=server_id, name=server_id, runner="npx",
        command="npx", args=[], env={},
    )
    with Session(engine) as session:
        return repo.create_server(session, server)


def _mint_token(engine, scope: str) -> str:
    raw = new_token()
    with Session(engine) as session:
        repo.create_token(
            session,
            Token(id=new_id(), name="t", token_hash=hash_token(raw), prefix=raw[:12], scope=scope),
        )
    return raw


async def test_all_scope_token_authorizes_any_server(bearer_engine):
    raw = _mint_token(bearer_engine, "all")
    # No exception == authorized, for two distinct servers.
    await BearerProvider().authenticate(_bearer_request(raw), _make_server(bearer_engine, "srv-a"))
    await BearerProvider().authenticate(_bearer_request(raw), _make_server(bearer_engine, "srv-b"))


async def test_scoped_token_authorizes_only_its_server(bearer_engine):
    srv_a = _make_server(bearer_engine, "srv-a")
    srv_b = _make_server(bearer_engine, "srv-b")
    raw = _mint_token(bearer_engine, "srv-a")

    await BearerProvider().authenticate(_bearer_request(raw), srv_a)  # its server: ok

    with pytest.raises(HTTPException) as exc:  # another server: rejected
        await BearerProvider().authenticate(_bearer_request(raw), srv_b)
    assert exc.value.status_code == 403


async def test_invalid_token_still_rejected_401(bearer_engine):
    srv = _make_server(bearer_engine, "srv-a")
    _mint_token(bearer_engine, "all")  # a valid token exists, but we send a bogus one
    with pytest.raises(HTTPException) as exc:
        await BearerProvider().authenticate(_bearer_request("not-a-real-token"), srv)
    assert exc.value.status_code == 401
