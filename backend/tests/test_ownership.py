"""Multi-user ownership: visibility, runner policy, and token scoping.

One policy module (`app.auth.policy`) drives every rule these tests probe:
members see exactly the servers/tokens they own (others 404, indistinguishable
from nonexistent), local runners are permission-gated, and data-plane tokens can
only be minted for owned servers.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from conftest import LOOPBACK, mint_token as _mint

from app.db import get_engine
from app.db.models import Server, Token, User
from app.main import app
from app.registry import settings as runtime_settings

REMOTE = {"runner": "remote", "command": "http://127.0.0.1:9/mcp"}


def _reset() -> None:
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "auto", "bind_mode": "local"})
        s.execute(delete(Token))
        s.execute(delete(User))
        s.execute(delete(Server))
        s.commit()


def _bearer(token: str) -> dict[str, str]:
    return {**LOOPBACK, "authorization": f"Bearer {token}"}


def _setup(client, *, local_runners: bool = False):
    """Enforcement on; returns (admin_headers, member_headers, member_user_id)."""
    admin_token = _mint("control")
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    admin = _bearer(admin_token)
    user = client.post(
        "/api/users",
        json={"name": "Mel", "role": "member", "local_runners": local_runners},
        headers=admin,
    ).json()
    cred = client.post(f"/api/users/{user['id']}/credentials", headers=admin).json()["token"]
    return admin, _bearer(cred), user["id"]


def _mk(client, headers, name, **overrides) -> dict:
    payload = {"name": name, **REMOTE, **overrides}
    r = client.post("/api/servers", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_visibility_is_ownership_scoped():
    with TestClient(app) as client:
        try:
            admin, member, uid = _setup(client)
            theirs = _mk(client, admin, "admins server")
            mine = _mk(client, member, "members server")
            assert mine["owner_id"] == uid

            # Lists: the member sees exactly their server; the admin sees both
            # (with the member's row labeled by owner).
            member_ids = [s["id"] for s in client.get("/api/servers", headers=member).json()]
            assert member_ids == [mine["id"]]
            admin_rows = {s["id"]: s for s in client.get("/api/servers", headers=admin).json()}
            assert set(admin_rows) == {theirs["id"], mine["id"]}
            assert admin_rows[mine["id"]]["owner_name"] == "Mel"

            # Every per-server route 404s for a non-visible id — same as nonexistent.
            for method, path in [
                ("get", f"/api/servers/{theirs['id']}"),
                ("patch", f"/api/servers/{theirs['id']}"),
                ("delete", f"/api/servers/{theirs['id']}"),
                ("post", f"/api/servers/{theirs['id']}/enable"),
                ("post", f"/api/servers/{theirs['id']}/disable"),
                ("post", f"/api/servers/{theirs['id']}/clone"),
                ("get", f"/api/servers/{theirs['id']}/logs"),
            ]:
                kwargs = {"json": {}} if method in ("patch",) else {}
                r = getattr(client, method)(path, headers=member, **kwargs)
                assert r.status_code == 404, (path, r.status_code)

            # Health mirrors visibility: summary is scoped, foreign slugs 404.
            slugs = [s["slug"] for s in client.get("/api/health/summary", headers=member).json()["servers"]]
            assert slugs == [mine["slug"]]
            assert client.get(f"/api/health/{theirs['slug']}", headers=member).status_code == 404

            # The admin can see and edit the member's server.
            assert client.get(f"/api/servers/{mine['id']}", headers=admin).status_code == 200
        finally:
            _reset()


def test_local_runner_permission():
    with TestClient(app) as client:
        try:
            admin, member, uid = _setup(client, local_runners=False)
            # Local runners are refused; remote is always allowed.
            r = client.post(
                "/api/servers", json={"name": "npx", "command": "echo"}, headers=member
            )
            assert r.status_code == 403
            assert _mk(client, member, "remote ok")

            # Grant the permission -> npx create succeeds and is owned.
            client.patch(f"/api/users/{uid}", json={"local_runners": True}, headers=admin)
            r = client.post(
                "/api/servers", json={"name": "npx", "command": "echo"}, headers=member
            )
            assert r.status_code == 201 and r.json()["owner_id"] == uid

            # Revoke again: the member may still toggle the existing server but not
            # reshape what it executes (or clone it).
            client.patch(f"/api/users/{uid}", json={"local_runners": False}, headers=admin)
            sid = r.json()["id"]
            assert client.post(f"/api/servers/{sid}/disable", headers=member).status_code == 200
            assert client.patch(
                f"/api/servers/{sid}", json={"command": "evil"}, headers=member
            ).status_code == 403
            assert client.patch(
                f"/api/servers/{sid}", json={"name": "renamed"}, headers=member
            ).status_code == 200  # non-launch fields stay editable
            # The SPA's edit form resends the whole config: unchanged launch VALUES
            # must not trip the gate (presence != change).
            assert client.patch(
                f"/api/servers/{sid}",
                json={"name": "again", "runner": "npx", "command": "echo", "args": [], "env": {}},
                headers=member,
            ).status_code == 200
            assert client.post(f"/api/servers/{sid}/clone", headers=member).status_code == 403
        finally:
            _reset()


def test_admin_local_runner_permission_is_effective():
    """policy always permits every runner for admins; the principal's
    local_runners field must agree (an admin row storing false is irrelevant),
    or the SPA would render a crippled form."""
    with TestClient(app) as client:
        try:
            admin, _, _ = _setup(client)
            a = client.post(
                "/api/users",
                json={"name": "Ann", "role": "admin", "local_runners": False},
                headers=admin,
            ).json()
            cred = client.post(f"/api/users/{a['id']}/credentials", headers=admin).json()["token"]
            ann = _bearer(cred)
            assert client.get("/api/auth/status", headers=ann).json()["user"]["local_runners"] is True
            assert client.post(
                "/api/servers", json={"name": "npx ok", "command": "echo"}, headers=ann
            ).status_code == 201
        finally:
            _reset()


def test_reassignment_cancels_pending_oauth(monkeypatch):
    """An in-flight upstream-OAuth authorization belongs to the former owner —
    transfer must cancel it so a late callback can't promote their grant onto
    the reassigned server."""
    from app.api import servers as servers_api

    cancelled: list[str] = []
    monkeypatch.setattr(servers_api.oauth_flow, "cancel_pending", cancelled.append)
    with TestClient(app) as client:
        try:
            admin, member, uid = _setup(client)
            theirs = _mk(client, admin, "transferring")
            r = client.patch(
                f"/api/servers/{theirs['id']}", json={"owner_id": uid}, headers=admin
            )
            assert r.status_code == 200
            assert theirs["id"] in cancelled
        finally:
            _reset()


def test_member_cannot_convert_remote_to_local():
    with TestClient(app) as client:
        try:
            _, member, _ = _setup(client, local_runners=False)
            mine = _mk(client, member, "mine")
            r = client.patch(
                f"/api/servers/{mine['id']}",
                json={"runner": "npx", "command": "echo"},
                headers=member,
            )
            assert r.status_code == 403
        finally:
            _reset()


def test_owner_reassignment_is_admin_only():
    with TestClient(app) as client:
        try:
            admin, member, uid = _setup(client)
            theirs = _mk(client, admin, "reassign me")
            # Member can't grab ownership (404: the row isn't visible to them at all).
            assert client.patch(
                f"/api/servers/{theirs['id']}", json={"owner_id": uid}, headers=member
            ).status_code == 404
            # Member can't give THEIR server away either (403: visible, not allowed).
            mine = _mk(client, member, "mine")
            assert client.patch(
                f"/api/servers/{mine['id']}", json={"owner_id": None}, headers=member
            ).status_code == 403
            # Admin reassigns; the member now sees it.
            r = client.patch(
                f"/api/servers/{theirs['id']}", json={"owner_id": uid}, headers=admin
            )
            assert r.status_code == 200 and r.json()["owner_id"] == uid
            assert client.get(f"/api/servers/{theirs['id']}", headers=member).status_code == 200
            # Unknown user id is a 400, not a silent orphan — and the rejection is
            # ATOMIC: config changes riding the same PATCH must not commit first.
            r = client.patch(
                f"/api/servers/{mine['id']}",
                json={"name": "half-applied", "owner_id": "nope"},
                headers=admin,
            )
            assert r.status_code == 400
            assert (
                client.get(f"/api/servers/{mine['id']}", headers=admin).json()["name"]
                == "mine"
            )

            # Reassigning AWAY revokes the former owner's data-plane tokens for
            # that server (their access is gone; their tokens must not linger),
            # while an admin-minted token for the same server survives.
            member_tok = client.post(
                "/api/tokens", json={"name": "m", "scope": mine["id"]}, headers=member
            ).json()
            admin_tok = client.post(
                "/api/tokens", json={"name": "a", "scope": mine["id"]}, headers=admin
            ).json()
            r = client.patch(
                f"/api/servers/{mine['id']}", json={"owner_id": None}, headers=admin
            )
            assert r.status_code == 200 and r.json()["owner_id"] is None
            remaining = {t["id"] for t in client.get("/api/tokens", headers=admin).json()}
            assert member_tok["id"] not in remaining
            assert admin_tok["id"] in remaining
        finally:
            _reset()


def test_token_minting_policy():
    with TestClient(app) as client:
        try:
            admin, member, uid = _setup(client)
            theirs = _mk(client, admin, "theirs")
            mine = _mk(client, member, "mine")

            # Named scopes are admin-only for members...
            for scope in ("all", "control", "group:all"):
                r = client.post(
                    "/api/tokens", json={"name": "t", "scope": scope}, headers=member
                )
                assert r.status_code == 403, scope
            # ...a foreign server id is indistinguishable from a dangling one...
            r = client.post(
                "/api/tokens", json={"name": "t", "scope": theirs["id"]}, headers=member
            )
            assert r.status_code == 400
            # ...and an owned server id works, stamped with the minter.
            r = client.post(
                "/api/tokens", json={"name": "t", "scope": mine["id"]}, headers=member
            )
            assert r.status_code == 201 and r.json()["user_id"] == uid

            # Token visibility: the member sees only their OWN rows — the login
            # credential (control, user-bound) and the data-plane token just minted;
            # the admin sees all (the member's rows labeled with the owner's name).
            mine_listed = client.get("/api/tokens", headers=member).json()
            assert {t["user_id"] for t in mine_listed} == {uid} and len(mine_listed) == 2
            mine_listed = [t for t in mine_listed if t["scope"] != "control"]
            all_listed = client.get("/api/tokens", headers=admin).json()
            assert any(t["user_name"] == "Mel" for t in all_listed)

            # Deleting a foreign token 404s; deleting an owned one works.
            admin_token_id = next(t["id"] for t in all_listed if t["user_id"] is None)
            assert client.delete(f"/api/tokens/{admin_token_id}", headers=member).status_code == 404
            assert client.delete(f"/api/tokens/{mine_listed[0]['id']}", headers=member).status_code == 204
        finally:
            _reset()


def test_member_import_skips_local_entries():
    with TestClient(app) as client:
        try:
            _, member, uid = _setup(client, local_runners=False)
            payload = {
                "mcpServers": {
                    "loc": {"command": "npx", "args": ["-y", "x"]},
                    "rem": {"url": "http://127.0.0.1:9/mcp"},
                }
            }
            r = client.post("/api/servers/import", json=payload, headers=member)
            assert r.status_code == 201, r.text
            body = r.json()
            assert [s["name"] for s in body["created"]] == ["rem"]
            assert body["created"][0]["owner_id"] == uid
            assert [s["name"] for s in body["skipped"]] == ["loc"]
            assert "local-runner" in body["skipped"][0]["reason"]
        finally:
            _reset()


def test_permitted_member_import_owns_local_entries():
    """Regression: the local-import branch must stamp the importer as owner too —
    a NULL owner would make the created row vanish from the member's own list."""
    with TestClient(app) as client:
        try:
            _, member, uid = _setup(client, local_runners=True)
            payload = {"mcpServers": {"loc": {"command": "npx", "args": ["-y", "x"]}}}
            r = client.post("/api/servers/import", json=payload, headers=member)
            assert r.status_code == 201, r.text
            created = r.json()["created"]
            assert [s["name"] for s in created] == ["loc"]
            assert created[0]["owner_id"] == uid
            assert client.get(
                f"/api/servers/{created[0]['id']}", headers=member
            ).status_code == 200
        finally:
            _reset()
