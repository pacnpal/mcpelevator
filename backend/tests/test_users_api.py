"""User management: admin-only CRUD, login credentials, and the lock-out guards.

Users hold no passwords — an admin mints them a control token bound via
``Token.user_id``. These tests drive the whole lifecycle over the API with
enforcement ON (a legacy user-less control token acts as the admin, proving the
upgrade path keeps full power).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from conftest import LOOPBACK, mint_token as _mint

from app.db import get_engine, repo
from app.db.models import Server, Token, User
from app.main import app
from app.registry import settings as runtime_settings


def _reset() -> None:
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "auto", "bind_mode": "local"})
        s.execute(delete(Token))
        s.execute(delete(User))
        s.execute(delete(Server))
        s.commit()


def _bearer(token: str) -> dict[str, str]:
    return {**LOOPBACK, "authorization": f"Bearer {token}"}


def _enforce() -> dict[str, str]:
    """Turn enforcement on and return admin headers (a legacy user-less control
    token — the pre-multi-user credential shape, which must stay all-powerful)."""
    admin = _mint("control")
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    return _bearer(admin)


def test_user_lifecycle_and_login():
    with TestClient(app) as client:
        try:
            admin = _enforce()
            # create
            r = client.post(
                "/api/users", json={"name": "Ada", "role": "member"}, headers=admin
            )
            assert r.status_code == 201, r.text
            user = r.json()
            assert (user["role"], user["local_runners"]) == ("member", False)  # safe default

            # mint a login credential and sign in with it
            r = client.post(f"/api/users/{user['id']}/credentials", headers=admin)
            assert r.status_code == 201
            cred = r.json()["token"]
            status = client.get("/api/auth/status", headers=_bearer(cred)).json()
            assert status["authenticated"] is True
            assert status["user"] == {
                "id": user["id"],
                "name": "Ada",
                "role": "member",
                "local_runners": False,
            }

            # patch: rename + grant local runners
            r = client.patch(
                f"/api/users/{user['id']}",
                json={"name": "Ada L.", "local_runners": True},
                headers=admin,
            )
            assert r.status_code == 200
            assert (r.json()["name"], r.json()["local_runners"]) == ("Ada L.", True)

            # the users list reflects token counts (the login credential)
            listed = client.get("/api/users", headers=admin).json()
            assert [u["tokens_count"] for u in listed] == [1]

            # delete: the credential is revoked with the user
            assert client.delete(f"/api/users/{user['id']}", headers=admin).status_code == 204
            assert client.get("/api/servers", headers=_bearer(cred)).status_code == 401
        finally:
            _reset()


def test_member_cannot_reach_user_management():
    with TestClient(app) as client:
        try:
            admin = _enforce()
            uid = client.post(
                "/api/users", json={"name": "Mel", "role": "member"}, headers=admin
            ).json()["id"]
            cred = client.post(f"/api/users/{uid}/credentials", headers=admin).json()["token"]
            member = _bearer(cred)
            assert client.get("/api/users", headers=member).status_code == 403
            assert client.post(
                "/api/users", json={"name": "x"}, headers=member
            ).status_code == 403
            # groups and settings writes are admin-only too
            assert client.get("/api/groups", headers=member).status_code == 403
            assert client.patch(
                "/api/settings", json={"idle_timeout_s": 0}, headers=member
            ).status_code == 403
            # ...but settings READ works (the add-server form needs it)
            assert client.get("/api/settings", headers=member).status_code == 200
        finally:
            _reset()


def test_delete_refused_while_user_owns_servers():
    with TestClient(app) as client:
        try:
            admin = _enforce()
            uid = client.post(
                "/api/users", json={"name": "Owner", "role": "member"}, headers=admin
            ).json()["id"]
            cred = client.post(f"/api/users/{uid}/credentials", headers=admin).json()["token"]
            member = _bearer(cred)
            r = client.post(
                "/api/servers",
                json={"name": "mine", "runner": "remote", "command": "http://127.0.0.1:9/mcp"},
                headers=member,
            )
            assert r.status_code == 201, r.text
            server_id = r.json()["id"]

            assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 409
            assert client.delete(f"/api/servers/{server_id}", headers=admin).status_code == 204
            assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 204
        finally:
            _reset()


def test_last_admin_credential_guard():
    """Demoting or deleting an admin is refused when no other admin login would
    remain; adding a second admin credential (or a legacy user-less control token)
    lifts the guard."""
    with TestClient(app) as client:
        try:
            # No legacy tokens in this test: manage users while enforcement is OFF
            # (the synthetic local admin), then reason about DB credentials only.
            a = client.post(
                "/api/users", json={"name": "A", "role": "admin"}, headers=LOOPBACK
            ).json()
            client.post(f"/api/users/{a['id']}/credentials", headers=LOOPBACK)

            # A's credential is the only admin login -> demote/delete refused.
            assert client.patch(
                f"/api/users/{a['id']}", json={"role": "member"}, headers=LOOPBACK
            ).status_code == 409
            assert client.delete(f"/api/users/{a['id']}", headers=LOOPBACK).status_code == 409

            # A second admin WITH a credential lifts it.
            b = client.post(
                "/api/users", json={"name": "B", "role": "admin"}, headers=LOOPBACK
            ).json()
            # ...but only once B can actually log in:
            assert client.patch(
                f"/api/users/{a['id']}", json={"role": "member"}, headers=LOOPBACK
            ).status_code == 409
            client.post(f"/api/users/{b['id']}/credentials", headers=LOOPBACK)
            assert client.patch(
                f"/api/users/{a['id']}", json={"role": "member"}, headers=LOOPBACK
            ).status_code == 200
        finally:
            _reset()


def test_demotion_revokes_overprivileged_tokens():
    """admin -> member revokes the data-plane tokens only an admin could mint
    ("all", group scopes, foreign-server scopes) while keeping the login token
    and tokens for servers the user owns."""
    with TestClient(app) as client:
        try:
            admin = _enforce()
            a = client.post(
                "/api/users", json={"name": "Ann", "role": "admin"}, headers=admin
            ).json()
            cred = client.post(f"/api/users/{a['id']}/credentials", headers=admin).json()["token"]
            ann = _bearer(cred)
            # Ann (as admin) mints an all-scoped token and one for a server she owns.
            r = client.post(
                "/api/servers",
                json={"name": "anns", "runner": "remote", "command": "http://127.0.0.1:9/mcp"},
                headers=ann,
            )
            own_server = r.json()["id"]
            wide = client.post(
                "/api/tokens", json={"name": "wide", "scope": "all"}, headers=ann
            ).json()
            own = client.post(
                "/api/tokens", json={"name": "own", "scope": own_server}, headers=ann
            ).json()

            r = client.patch(f"/api/users/{a['id']}", json={"role": "member"}, headers=admin)
            assert r.status_code == 200, r.text
            remaining = {t["id"] for t in client.get("/api/tokens", headers=admin).json()}
            assert wide["id"] not in remaining  # admin-grade scope: revoked
            assert own["id"] in remaining  # a member could mint this: kept
            # The login credential survives and now resolves to the member role.
            status = client.get("/api/auth/status", headers=ann).json()
            assert status["authenticated"] is True and status["user"]["role"] == "member"
        finally:
            _reset()


def test_member_login_token_does_not_satisfy_last_control_guard():
    """Regression: the token-delete guard must protect the last ADMIN credential.
    A member's login token is also control-scoped, but it must not count — deleting
    the last admin token while only member logins remain would strand the box."""
    with TestClient(app) as client:
        try:
            admin = _enforce()  # one legacy (user-less => admin) control token
            uid = client.post(
                "/api/users", json={"name": "Mel", "role": "member"}, headers=admin
            ).json()["id"]
            client.post(f"/api/users/{uid}/credentials", headers=admin)  # member login token

            tokens = client.get("/api/tokens", headers=admin).json()
            admin_token_id = next(t["id"] for t in tokens if t["user_id"] is None)
            # The member's control token exists, but deleting the only ADMIN
            # credential is still refused.
            r = client.delete(f"/api/tokens/{admin_token_id}", headers=admin)
            assert r.status_code == 409

            # A second ADMIN credential (an admin user with a login token) lifts it.
            aid = client.post(
                "/api/users", json={"name": "Ann", "role": "admin"}, headers=admin
            ).json()["id"]
            client.post(f"/api/users/{aid}/credentials", headers=admin)
            assert client.delete(
                f"/api/tokens/{admin_token_id}", headers=admin
            ).status_code == 204
        finally:
            _reset()


def test_dangling_user_credential_fails_closed():
    """A control token whose user row is gone must not authenticate (the gate and
    principal resolution agree)."""
    with TestClient(app) as client:
        try:
            admin = _enforce()
            uid = client.post(
                "/api/users", json={"name": "Ghost", "role": "admin"}, headers=admin
            ).json()["id"]
            cred = client.post(f"/api/users/{uid}/credentials", headers=admin).json()["token"]
            # Simulate a hand-edited DB: remove the user row but keep the token.
            with Session(get_engine()) as s:
                user = repo.get_user(s, uid)
                s.delete(user)
                s.commit()
            assert client.get("/api/servers", headers=_bearer(cred)).status_code == 401
        finally:
            _reset()
