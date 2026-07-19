"""Per-request control-plane auth: the bearer-token layer on /api.

Proves the second layer on top of the Host/Origin allowlist: when enforcement is
on (expose under `auto`, or `always`), the sensitive /api routers require a token
with the `control` scope, while `/api/health` and `/api/auth/status` stay public
and a fresh local install needs no token at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session

from conftest import LOOPBACK, mint_token as _mint

from app.db import get_engine, init_db, repo
from app.main import app
from app.registry import settings as runtime_settings


def _reset() -> None:
    """Back to a clean default (local/auto, no allowlist, no tokens) so the shared
    engine doesn't leak state (or a bootstrapped admin token) into later tests."""
    with Session(get_engine()) as s:
        runtime_settings.write(
            s,
            {
                "bind_mode": "local",
                "allowed_hosts": [],
                "control_plane_auth": "auto",
                "allow_private_lan": False,
            },
        )
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
            control, data = _mint("control"), _mint("all")  # "all" = a data-plane token
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"bind_mode": "expose"})
            assert client.get("/api/servers", headers=LOOPBACK).status_code == 401  # no token
            assert client.get("/api/servers", headers=_bearer(data)).status_code == 403  # data-plane token rejected on /api
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
            # Enforcement off: not "authenticated" (no token), but the principal is
            # the synthetic local admin so the SPA renders the full surface.
            body = client.get("/api/auth/status", headers=LOOPBACK).json()
            assert (body["enforced"], body["authenticated"]) == (False, False)
            assert body["user"] == {
                "id": None,
                "name": "local operator",
                "role": "admin",
                "local_runners": True,
            }
            control = _mint("control")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})
            body = client.get("/api/auth/status", headers=LOOPBACK).json()
            assert (body["enforced"], body["authenticated"]) == (True, False)
            assert body["user"] is None  # no credential, enforcement on -> no principal
            body = client.get("/api/auth/status", headers=_bearer(control)).json()
            assert (body["enforced"], body["authenticated"]) == (True, True)
            # A user-less control token (boot mint / pre-multi-user) is an admin.
            assert body["user"]["role"] == "admin" and body["user"]["id"] is None
        finally:
            _reset()


def test_break_glass_admin_token(monkeypatch):
    from types import SimpleNamespace

    from app.auth import control_plane

    monkeypatch.setattr(
        control_plane,
        "get_settings",
        lambda: SimpleNamespace(
            admin_token="mcpe_break_glass",
            base_url="http://127.0.0.1:8080",
            public_host=None,
            extra_allowed_hosts=[],
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


def test_bootstrap_prints_recovery_notice_when_token_already_exists(capsys):
    """When enforcement turns on but a control token already exists, we can't reprint
    its plaintext — but the LAN seed promises an admin-token notice, so the bootstrap
    must still print a notice (pointing at recovery) instead of silently swallowing it."""
    init_db()
    try:
        _mint("control")  # a token already exists; its plaintext is not held here
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": "always"})
        capsys.readouterr()  # drop anything printed during setup
        with TestClient(app):  # lifespan runs _bootstrap_control_plane_auth
            pass
        out = capsys.readouterr().out
        assert "control-plane auth is ON" in out
        assert "An admin token already exists" in out
        assert "Settings → Security" in out
    finally:
        _reset()


def test_bootstrap_force_mints_a_fresh_token_when_env_set(monkeypatch, capsys):
    """MCPE_MINT_ADMIN_TOKEN recovers a headless box whose admin token was lost: even
    though a control token already exists, the bootstrap mints a fresh one and prints
    it (existing tokens keep working — it only adds one)."""
    from types import SimpleNamespace

    from app import main
    from app.auth import control_plane

    init_db()
    try:
        _mint("control")  # one already exists -> ensure_control_token() would mint nothing
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": "always"})
            before = len([t for t in repo.list_tokens(s) if t.scope == "control"])
        monkeypatch.setattr(
            control_plane,
            "get_settings",
            lambda: SimpleNamespace(public_host=None, admin_token=None, extra_allowed_hosts=[]),
        )
        monkeypatch.setattr(
            main,
            "get_settings",
            lambda: SimpleNamespace(
                admin_token=None, mint_admin_token=True, base_url="http://127.0.0.1:8080"
            ),
        )
        capsys.readouterr()  # drop setup output
        main._bootstrap_control_plane_auth()
        out = capsys.readouterr().out
        with Session(get_engine()) as s:
            after = len([t for t in repo.list_tokens(s) if t.scope == "control"])
        assert after == before + 1  # a fresh token was minted despite one already existing
        assert "Admin token (shown once" in out
        assert "MCPE_MINT_ADMIN_TOKEN" in out
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


def test_allow_private_lan_enforces_under_auto():
    """Opening to the LAN makes the box reachable off-host, so `auto` must enforce
    even while bind_mode stays `local` — otherwise the LAN would reach /api
    unauthenticated."""
    from app.auth.control_plane import enforcement_enabled

    init_db()
    try:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"control_plane_auth": "auto", "bind_mode": "local"})
            assert enforcement_enabled(s) is False  # zero-config loopback install
            runtime_settings.write(s, {"allow_private_lan": True})
            assert enforcement_enabled(s) is True  # LAN open -> token required
    finally:
        _reset()


def test_allow_private_lan_env_seeds_on_first_boot(monkeypatch):
    """A headless box can enable LAN access via MCPE_ALLOW_PRIVATE_LAN: on first boot
    (setting never written) the env seeds it true, and enforcement turns on so the
    bootstrap mints/prints an admin token the operator reads from the logs."""
    from types import SimpleNamespace

    from app import main
    from app.auth.control_plane import enforcement_enabled
    from app.db.models import Setting

    init_db()
    try:
        with Session(get_engine()) as s:  # simulate "never written"
            row = s.get(Setting, "allow_private_lan")
            if row is not None:
                s.delete(row)
                s.commit()
        monkeypatch.setattr(
            main, "get_settings", lambda: SimpleNamespace(allow_private_lan=True, port=8080)
        )
        main._bootstrap_private_lan()
        with Session(get_engine()) as s:
            assert runtime_settings.allow_private_lan(s) is True
            assert enforcement_enabled(s) is True  # LAN open -> /api now requires a token
    finally:
        _reset()


def test_allow_private_lan_env_does_not_override_a_user_choice(monkeypatch):
    """The env is a first-boot seed only: if the setting was already written (e.g. the
    operator turned it off in the UI), a later boot with the env set must not re-enable."""
    from types import SimpleNamespace

    from app import main

    init_db()
    try:
        with Session(get_engine()) as s:
            runtime_settings.write(s, {"allow_private_lan": False})  # explicit user choice
        monkeypatch.setattr(
            main, "get_settings", lambda: SimpleNamespace(allow_private_lan=True, port=8080)
        )
        main._bootstrap_private_lan()  # row exists -> no reseed
        with Session(get_engine()) as s:
            assert runtime_settings.allow_private_lan(s) is False
    finally:
        _reset()


def test_enabling_private_lan_without_credential_is_refused():
    """Enabling allow_private_lan turns enforcement on (auto), so the PATCH must carry
    a control credential or it would lock the operator out — same guard as expose."""
    with TestClient(app) as client:
        try:
            assert client.patch(
                "/api/settings", json={"allow_private_lan": True}, headers=LOOPBACK
            ).status_code == 400
            control = _mint("control")
            assert client.patch(
                "/api/settings", json={"allow_private_lan": True}, headers=_bearer(control)
            ).status_code == 200
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
                lambda: SimpleNamespace(public_host=None, admin_token=None, extra_allowed_hosts=[]),
            )
            assert control_plane.enforcement_enabled(s) is False  # local + no public URL -> zero-config
            monkeypatch.setattr(
                control_plane, "get_settings",
                lambda: SimpleNamespace(
                    public_host="mcp.example.com", admin_token=None, extra_allowed_hosts=[]
                ),
            )
            assert control_plane.enforcement_enabled(s) is True  # public URL -> enforced
            # MCPE_ALLOWED_HOSTS alone (no public URL) also makes the box reachable off-host
            # via that hostname, so `auto` must enforce too — the P1 fix.
            monkeypatch.setattr(
                control_plane, "get_settings",
                lambda: SimpleNamespace(
                    public_host=None, admin_token=None, extra_allowed_hosts=["mcp.example.com"]
                ),
            )
            assert control_plane.enforcement_enabled(s) is True  # env-allowed host -> enforced
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


def test_bearer_provider_rejects_control_token_on_data_plane():
    """Scopes don't cross: on /s the per-server bearer check accepts an `all` (or a
    matching server-id) token, but a `control` admin token is rejected even though both
    live in one table."""
    import asyncio
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from app.auth.bearer import BearerProvider
    from app.db.models import Server

    init_db()
    try:
        all_token, control = _mint("all"), _mint("control")
        provider = BearerProvider()
        server = Server(id="x", slug="x", name="x", runner="npx", command="npx", args=[], env={})

        def req(token: str):
            return SimpleNamespace(headers={"authorization": f"Bearer {token}"})

        asyncio.run(provider.authenticate(req(all_token), server))  # "all" scope: accepted
        with pytest.raises(HTTPException) as exc:
            asyncio.run(provider.authenticate(req(control), server))  # control token: rejected
        assert exc.value.status_code == 403
    finally:
        _reset()


def test_inventory_health_requires_control_token_when_enforced():
    """Inventory-bearing health endpoints must not disclose slugs or slug existence
    without a control token when control-plane auth is enforced."""
    with TestClient(app) as client:
        try:
            control = _mint("control")
            with Session(get_engine()) as s:
                runtime_settings.write(s, {"control_plane_auth": "always"})

            assert client.get("/api/health/summary", headers=LOOPBACK).status_code == 401
            assert client.get("/api/health/nonexistent-slug-xyz", headers=LOOPBACK).status_code == 401

            assert client.get("/api/health/summary", headers=_bearer(control)).status_code == 200
            assert client.get("/api/health/nonexistent-slug-xyz", headers=_bearer(control)).status_code == 404
        finally:
            _reset()
