"""Deactivated users (#278): an admin deactivates a User, which ends their Sign-in
sessions and revokes every token they hold, while their record and servers stay. A
Deactivated user resolves to no principal and can't sign in through SSO, even when the
provider lets them in. Reactivating restores sign-in; revoked tokens stay revoked."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from conftest import LOOPBACK, create_server

# idp is the fixture; the rest are the sign-in helpers.
from test_sso_sign_in import (
    _echo_headers,
    _enforce,
    _reason,
    _session_headers,
    _sessions,
    _settings,
    _sign_in,
    _signed_in,
    _user,
    idp,
)

from app.auth import principal as principal_mod
from app.db import get_engine
from app.db.models import Server, Token, User
from app.main import app
from app.util import hash_token, new_id, new_token


def _token(user_id: str, scope: str) -> str:
    raw = new_token()
    with Session(get_engine()) as s:
        s.add(Token(id=new_id(), name=scope, token_hash=hash_token(raw), prefix=raw[:12],
                    scope=scope, user_id=user_id))
        s.commit()
    return raw


def _bearer(raw: str) -> dict[str, str]:
    return {**LOOPBACK, "authorization": f"Bearer {raw}"}


def _deactivate(client: TestClient, admin: dict, uid: str, active: bool = False) -> httpx.Response:
    return client.patch(f"/api/users/{uid}", json={"active": active}, headers=admin)


def _get_user(uid: str) -> User:
    with Session(get_engine()) as s:
        return s.get(User, uid)


def test_deactivating_stops_every_credential_and_keeps_the_server(idp):
    uid = _user("ada@example.com", role="admin")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        srv = create_server(client, name="hers", auth="bearer")
        admin = _enforce()
        with Session(get_engine()) as s:
            row = s.get(Server, srv["id"])
            row.owner_id = uid
            s.add(row)
            s.commit()
        _settings(groups={"team": "*"}, default_auth_provider="bearer")
        client.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_echo_headers))
        client.app.state.supervisor.endpoint = lambda slug: ("backend", 9000)
        client.app.state.supervisor.on_converged = None
        hub = client.app.state.groups
        prev = hub.app_for
        hub.app_for = lambda n: _echo_headers if n == "team" else prev(n)

        login = _bearer(_token(uid, "control"))
        data = _bearer(_token(uid, srv["id"]))
        group = _bearer(_token(uid, "group:team"))
        assert _signed_in(_sign_in(client, idp))
        session = _session_headers(client)
        assert client.get("/api/servers", headers=login).status_code == 200
        assert client.get("/api/servers", headers=session).status_code == 200
        assert client.get(f"/s/{srv['slug']}/mcp", headers=data).status_code == 200
        assert client.post("/g/team/mcp", headers=group).status_code == 200

        r = _deactivate(client, admin, uid)
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False and r.json()["tokens_count"] == 0

        assert client.get("/api/servers", headers=login).status_code == 401
        assert client.get("/api/servers", headers=session).status_code == 401
        assert client.get(f"/s/{srv['slug']}/mcp", headers=data).status_code == 401
        assert client.post("/g/team/mcp", headers=group).status_code == 401
        assert _sessions() == 0

        # The server stays theirs, and an admin's token still reaches it.
        all_admin = _bearer(_token(None, "all"))
        assert client.get(f"/s/{srv['slug']}/mcp", headers=all_admin).status_code == 200
        listed = client.get("/api/servers", headers=admin).json()
        assert [s["owner_id"] for s in listed if s["id"] == srv["id"]] == [uid]
        [info] = [u for u in client.get("/api/users", headers=admin).json() if u["id"] == uid]
        assert info["active"] is False and info["servers_count"] == 1


def test_a_deactivated_user_is_refused_at_sign_in_by_pair(idp):
    uid = _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        admin = _enforce()
        assert _signed_in(_sign_in(client, idp))  # links the pair
        assert _deactivate(client, admin, uid).status_code == 200
        assert _reason(_sign_in(client, idp)) == "deactivated"
    assert _sessions() == 0


def test_a_deactivated_user_is_refused_by_name_and_never_replaced(idp):
    uid = _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    _settings(oidc_provisioning=True)
    with TestClient(app) as client:
        admin = _enforce()
        assert _deactivate(client, admin, uid).status_code == 200
        assert _reason(_sign_in(client, idp)) == "deactivated"
    with Session(get_engine()) as s:
        assert [u.id for u in s.exec(select(User)).all()] == [uid]
    assert _get_user(uid).oidc_subject is None  # not linked either
    assert _sessions() == 0


def test_deactivating_the_last_admin_is_refused(idp):
    uid = _user("ada@example.com", role="admin")
    with TestClient(app) as client:
        _enforce()  # a legacy control token: an admin login besides ada's
        ada = _bearer(_token(uid, "control"))
        with Session(get_engine()) as s:  # leave ada's login as the only admin one
            for t in s.exec(select(Token).where(Token.user_id.is_(None))).all():
                s.delete(t)
            s.commit()
        r = _deactivate(client, ada, uid)
        assert r.status_code == 409, r.text
        assert "last admin" in r.json()["detail"]
    assert _get_user(uid).active is True


def test_reactivating_restores_sign_in_but_not_the_tokens(idp):
    uid = _user("ada@example.com")
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        admin = _enforce()
        login = _bearer(_token(uid, "control"))
        assert _deactivate(client, admin, uid).status_code == 200
        assert _reason(_sign_in(client, idp)) == "deactivated"
        r = _deactivate(client, admin, uid, active=True)
        assert r.status_code == 200 and r.json()["active"] is True
        assert _signed_in(_sign_in(client, idp))
        assert client.get("/api/servers", headers=_session_headers(client)).status_code == 200
        assert client.get("/api/servers", headers=login).status_code == 401


def test_a_request_queued_across_a_deactivation_fails_closed(idp):
    uid = _user("ada@example.com")
    with TestClient(app) as client:
        admin = _enforce()
        with Session(get_engine()) as s:  # resolved at entry, before the deactivation
            queued = principal_mod.Principal(
                user_id=uid, name="ada@example.com", role="member", local_runners=False
            )
            assert principal_mod.refresh(s, queued) is not None
        assert _deactivate(client, admin, uid).status_code == 200
        with Session(get_engine()) as s:
            assert principal_mod.refresh(s, queued) is None
