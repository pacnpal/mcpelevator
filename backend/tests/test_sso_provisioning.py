"""Just-in-time provisioning (#271): with ``oidc_provisioning`` on, a sign-in that
matches no User creates one, a Member with local runners off, linked at once."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import LOOPBACK

# idp is the fixture; the rest are the sign-in helpers.
from test_sso_sign_in import (
    _callback,
    _enforce,
    _reason,
    _session_headers,
    _sessions,
    _settings,
    _sign_in,
    _signed_in,
    _start,
    _user,
    idp,
)

from app.db import get_engine
from app.db.models import SignInSession, User
from app.main import app
from app.registry import settings as runtime_settings


def _users() -> list[User]:
    with Session(get_engine()) as s:
        return list(s.exec(select(User)).all())


def test_with_provisioning_off_no_match_is_refused(idp):
    idp.claims = {"email": "new@example.com"}
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "no_match"
    assert _users() == []


def test_provisioning_creates_a_linked_member_without_local_runners(idp):
    idp.claims = {"email": "New@Example.com"}
    _settings(oidc_provisioning=True)
    with TestClient(app) as client:
        _enforce()
        assert _signed_in(_sign_in(client, idp))
        me = client.get("/api/auth/status", headers=LOOPBACK).json()["user"]
        assert me["name"] == "New@Example.com" and me["role"] == "member"
        assert client.get("/api/servers", headers=_session_headers(client)).status_code == 200
    [user] = _users()
    assert (user.role, user.local_runners, user.control_mcp) == ("member", False, True)
    assert (user.oidc_issuer, user.oidc_subject) == (idp.issuer, "sub-1")


def test_a_provisioned_admin_group_member_is_admin(idp):
    idp.claims = {"email": "new@example.com", "groups": ["admins"]}
    _settings(oidc_provisioning=True, oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    [user] = _users()
    assert user.role == "admin"


def test_the_allowed_groups_gate_refuses_before_provisioning(idp):
    idp.claims = {"email": "new@example.com", "groups": ["contractors"]}
    _settings(oidc_provisioning=True, oidc_allowed_groups=["staff"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "not_allowed"
    assert _users() == []


def test_a_name_match_on_a_linked_user_is_still_a_conflict(idp):
    uid = _user("ada@example.com")
    with Session(get_engine()) as s:
        user = s.get(User, uid)
        user.oidc_issuer, user.oidc_subject = idp.issuer, "someone-else"
        s.add(user)
        s.commit()
    idp.claims = {"email": "ada@example.com"}
    _settings(oidc_provisioning=True)
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "identity_conflict"
    assert [u.id for u in _users()] == [uid]


def test_two_first_sign_ins_at_the_same_moment_provision_one_user(idp):
    idp.claims = {"email": "new@example.com"}
    _settings(oidc_provisioning=True)
    idp.token_barrier = threading.Barrier(2, timeout=10)  # both wait at the provider
    with TestClient(app):  # the app's lifespan; each racer request runs its own loop
        racers = [TestClient(app), TestClient(app)]
        queries = [idp.authorize(_start(c).headers["location"]) for c in racers]
        results: list[httpx.Response] = []
        threads = [
            threading.Thread(target=lambda c=c, q=q: results.append(_callback(c, q)))
            for c, q in zip(racers, queries)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert len(results) == 2
    assert all(_signed_in(r) for r in results), [r.headers.get("location") for r in results]
    [user] = _users()
    with Session(get_engine()) as s:
        assert [row.user_id for row in s.exec(select(SignInSession)).all()] == [user.id] * 2


def test_a_provisioned_member_adds_remote_servers_but_not_local_ones(idp):
    idp.claims = {"email": "new@example.com"}
    _settings(oidc_provisioning=True)
    with TestClient(app) as client:
        _enforce()
        assert _signed_in(_sign_in(client, idp))
        headers = _session_headers(client)
        remote = client.post("/api/servers", headers=headers, json={
            "name": "mine", "runner": "remote", "command": "http://127.0.0.1:9/mcp",
        })
        assert remote.status_code == 201, remote.text
        local = client.post("/api/servers", headers=headers, json={
            "name": "local", "runner": "npx", "command": "some-mcp-server",
        })
        assert local.status_code == 403, local.text
        me = client.get("/api/auth/status", headers=LOOPBACK).json()["user"]
        assert remote.json()["owner_id"] == me["id"]


def test_turning_provisioning_off_mid_flight_is_expired(idp):
    idp.claims = {"email": "new@example.com"}
    _settings(oidc_provisioning=True)
    answer = idp._token

    def slow_token(request):  # the admin turns provisioning off while this is pending
        _settings(oidc_provisioning=False)
        return answer(request)

    idp._token = slow_token
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "expired"
    assert _users() == []
    assert _sessions() == 0


def test_provisioning_is_off_by_default_validated_and_seeds_once(idp, monkeypatch):
    from app import main

    with TestClient(app) as client:
        assert client.get("/api/settings", headers=LOOPBACK).json()["oidc_provisioning"] is False
        r = client.patch("/api/settings", headers=LOOPBACK, json={"oidc_provisioning": "maybe"})
        assert r.status_code == 422
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(oidc_provisioning=True))
    main._bootstrap_oidc_provisioning()
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_provisioning"] is True
        runtime_settings.write(s, {"oidc_provisioning": False})
    main._bootstrap_oidc_provisioning()  # restart: already written, so the env is ignored
    with Session(get_engine()) as s:
        assert runtime_settings.read_all(s)["oidc_provisioning"] is False
