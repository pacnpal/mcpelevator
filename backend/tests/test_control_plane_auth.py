"""Per-request control-plane auth: the bearer-token layer on /api.

Proves the second layer on top of the Host/Origin allowlist: when enforcement is
on (expose under `auto`, or `always`), the sensitive /api routers require a token
with the `control` scope, while `/api/health` and `/api/auth/status` stay public
and a fresh local install needs no token at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine, init_db, repo
from app.db.models import Token
from app.main import app
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token

LOOPBACK = {"host": "127.0.0.1"}  # passes the allowlist (TestClient peer is loopback)


def _mint(scope: str) -> str:
    """Insert a token of the given scope into the shared DB; return the plaintext."""
    raw = new_token()
    with Session(get_engine()) as s:
        repo.create_token(
            s, Token(id=new_id(), name=scope, token_hash=hash_token(raw), prefix=raw[:12], scope=scope)
        )
    return raw


def _reset() -> None:
    """Back to a clean default (local/auto, no allowlist, no tokens) so the shared
    engine doesn't leak state (or a bootstrapped admin token) into later tests."""
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"bind_mode": "local", "allowed_hosts": [], "control_plane_auth": "auto"})
        for t in repo.list_tokens(s):
            repo.delete_token(s, t.id)


def _bearer(token: str) -> dict[str, str]:
    return {**LOOPBACK, "authorization": f"Bearer {token}"}


def test_local_auto_no_token_allows_control_plane():
    """Zero-config: the default (local + auto) does not enforce, so /api works
    with no token — the SPA keeps calling it unauthenticated."""
    with TestClient(app) as client:
        try:
            assert client.get("/api/servers", headers=LOOPBACK).status_code == 200
        finally:
            _reset()


def test_expose_auto_requires_control_token():
    with TestClient(app) as client:
        try:
            control, proxy = _mint("control"), _mint("proxy")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"bind_mode": "expose"})
            assert client.get("/api/servers", headers=LOOPBACK).status_code == 401  # no token
            assert client.get("/api/servers", headers=_bearer(proxy)).status_code == 403  # wrong scope
            assert client.get("/api/servers", headers=_bearer(control)).status_code == 200  # control token
        finally:
            _reset()


def test_always_enforces_in_local_mode():
    with TestClient(app) as client:
        try:
            control = _mint("control")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})
            assert client.get("/api/servers", headers=LOOPBACK).status_code == 401
            assert client.get("/api/servers", headers=_bearer(control)).status_code == 200
        finally:
            _reset()


def test_health_is_public_even_when_enforced():
    with TestClient(app) as client:
        try:
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})
            assert client.get("/api/health", headers=LOOPBACK).status_code == 200  # no token
        finally:
            _reset()


def test_auth_status_reflects_enforcement_and_credential():
    with TestClient(app) as client:
        try:
            assert client.get("/api/auth/status", headers=LOOPBACK).json() == {
                "enforced": False,
                "authenticated": False,
            }
            control = _mint("control")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})
            assert client.get("/api/auth/status", headers=LOOPBACK).json() == {
                "enforced": True,
                "authenticated": False,
            }
            assert client.get("/api/auth/status", headers=_bearer(control)).json() == {
                "enforced": True,
                "authenticated": True,
            }
        finally:
            _reset()


def test_break_glass_admin_token(monkeypatch):
    from types import SimpleNamespace

    from app.auth import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_settings",
        lambda: SimpleNamespace(
            admin_token="mcpe_break_glass", base_url="http://127.0.0.1:8080", public_host=None
        ),
    )
    with TestClient(app) as client:
        try:
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})
            assert client.get("/api/servers", headers=LOOPBACK).status_code == 401
            assert client.get("/api/servers", headers=_bearer("mcpe_break_glass")).status_code == 200
        finally:
            _reset()


def test_allowlist_runs_before_the_token_gate():
    """Defense in depth, in order: a bad Host is rejected by the allowlist middleware
    before the token gate runs, so a valid control token can't rescue it."""
    with TestClient(app) as client:
        try:
            control = _mint("control")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"bind_mode": "expose", "allowed_hosts": ["mcp.example.com"]})
            r = client.get("/api/servers", headers={"host": "evil.com", "authorization": f"Bearer {control}"})
            assert r.status_code == 403
        finally:
            _reset()


def test_startup_bootstrap_mints_when_enforced():
    """On boot with enforcement on and no token, lifespan mints one control token
    (the operator reads it from the logs) so a headless deployment isn't locked out."""
    init_db()
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    try:
        with TestClient(app):  # lifespan runs _bootstrap_control_plane_auth
            with Session(get_engine()) as s:
                assert repo.control_token_exists(s)
    finally:
        _reset()


def test_enforcement_enabled_matrix():
    from app.auth.control_plane import enforcement_enabled

    init_db()
    try:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": "auto", "bind_mode": "local"})
            assert enforcement_enabled(s) is False
            runtime_settings.write(s, {"bind_mode": "expose"})
            assert enforcement_enabled(s) is True
            runtime_settings.write(s, {"control_plane_auth": "always", "bind_mode": "local"})
            assert enforcement_enabled(s) is True
    finally:
        _reset()


def test_ensure_control_token_is_idempotent():
    from app.auth.control_plane import ensure_control_token

    init_db()
    try:
        with Session(get_engine()) as s:
            first = ensure_control_token(s)
            assert first and first.startswith("mcpe_")
            assert ensure_control_token(s) is None  # one already exists -> no second mint
            controls = [t for t in repo.list_tokens(s) if t.scope == "control"]
            assert len(controls) == 1
    finally:
        _reset()


def test_public_base_url_enforces_under_auto(monkeypatch):
    """A configured public URL is reachable off-host, so `auto` must enforce even
    while bind_mode stays `local` (request_allowlist already trusts that host)."""
    from types import SimpleNamespace

    from app.auth import control_plane

    init_db()
    try:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"bind_mode": "local", "control_plane_auth": "auto"})
            monkeypatch.setattr(
                control_plane, "get_settings",
                lambda: SimpleNamespace(public_host=None, admin_token=None),
            )
            assert control_plane.enforcement_enabled(s) is False  # local + no public URL -> zero-config
            monkeypatch.setattr(
                control_plane, "get_settings",
                lambda: SimpleNamespace(public_host="mcp.example.com", admin_token=None),
            )
            assert control_plane.enforcement_enabled(s) is True  # public URL -> enforced
    finally:
        _reset()


def test_settings_patch_rejects_enabling_auth_without_a_credential():
    """Enabling enforcement requires THIS request to authenticate as control, or the
    next request (including POST /tokens) would be gated and lock the operator out.
    A token row merely existing isn't enough: the caller must present it."""
    with TestClient(app) as client:
        try:
            # no credential at all -> rejected
            assert client.patch(
                "/api/settings", json={"control_plane_auth": "always"}, headers=LOOPBACK
            ).status_code == 400
            # a control token row exists but isn't presented on this request -> still rejected
            control = _mint("control")
            assert client.patch(
                "/api/settings", json={"bind_mode": "expose"}, headers=LOOPBACK
            ).status_code == 400
            assert client.get("/api/auth/status", headers=LOOPBACK).json()["enforced"] is False  # nothing applied
            # presenting the control token authorizes the change
            assert client.patch(
                "/api/settings", json={"control_plane_auth": "always"}, headers=_bearer(control)
            ).status_code == 200
        finally:
            _reset()


def test_cannot_revoke_last_control_token_while_enforced():
    """Revoking the only control token while enforcement is on would lock the operator
    out of /api (including minting a replacement), so it is refused with 409."""
    with TestClient(app) as client:
        try:
            control = _mint("control")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})
                cid = next(t.id for t in repo.list_tokens(s) if t.scope == "control")
            auth = _bearer(control)
            assert client.delete(f"/api/tokens/{cid}", headers=auth).status_code == 409  # last one, refused
            _mint("control")  # a second control token makes revoking the first safe
            assert client.delete(f"/api/tokens/{cid}", headers=auth).status_code == 204
        finally:
            _reset()


def test_revoke_last_control_token_allowed_when_not_enforced():
    """In local mode (not enforced) the last control token can be revoked: there's
    nothing to lock out of, so the guard predicate is False and the delete succeeds."""
    with TestClient(app) as client:
        try:
            _mint("control")
            with Session(get_engine()) as s:
                cid = next(t.id for t in repo.list_tokens(s) if t.scope == "control")
            assert client.delete(f"/api/tokens/{cid}", headers=LOOPBACK).status_code == 204
        finally:
            _reset()


def test_bearer_provider_requires_proxy_scope():
    """Scopes don't cross: the data-plane bearer check accepts only `proxy` tokens, so
    a `control` (admin) token can't be reused on /s even though both live in one table."""
    import asyncio
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from app.auth.bearer import BearerProvider
    from app.db.models import Server

    init_db()
    try:
        proxy, control = _mint("proxy"), _mint("control")
        provider = BearerProvider()
        server = Server(id="x", slug="x", name="x", runner="npx", command="npx", args=[], env={})

        def req(token: str):
            return SimpleNamespace(headers={"authorization": f"Bearer {token}"})

        asyncio.run(provider.authenticate(req(proxy), server))  # proxy token: accepted
        with pytest.raises(HTTPException) as exc:
            asyncio.run(provider.authenticate(req(control), server))  # control token: rejected
        assert exc.value.status_code == 403
    finally:
        _reset()
