"""SCIM provisioning (#279): an identity provider manages Users through /scim/v2 with a
token of the ``scim`` scope, which opens SCIM and nothing else. Bodies are shaped like
Entra ID's and Okta's provisioning calls (``scim_fixtures``)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import scim_fixtures as fx
from conftest import LOOPBACK, create_server, mint_token

# idp is the fixture (it also clears users, tokens, servers and settings afterwards).
from test_sso_sign_in import _enforce, _sign_in, _signed_in, _user, idp

from app.db import get_engine, repo
from app.db.models import Server, SignInSession, Token, User
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token

SCIM = "application/scim+json"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


def _headers(raw: str) -> dict[str, str]:
    return {**LOOPBACK, "authorization": f"Bearer {raw}", "content-type": SCIM}


def _scim() -> dict[str, str]:
    return _headers(mint_token("scim"))


def _token(user_id: str | None, scope: str) -> str:
    raw = new_token()
    with Session(get_engine()) as s:
        s.add(Token(id=new_id(), name=scope, token_hash=hash_token(raw), prefix=raw[:12],
                    scope=scope, user_id=user_id))
        s.commit()
    return raw


def _user_row(uid: str) -> User | None:
    with Session(get_engine()) as s:
        return s.get(User, uid)


def _tokens_of(uid: str) -> int:
    with Session(get_engine()) as s:
        return len(s.exec(select(Token).where(Token.user_id == uid)).all())


def _sessions_of(uid: str) -> int:
    with Session(get_engine()) as s:
        return len(s.exec(select(SignInSession).where(SignInSession.user_id == uid)).all())


def _owns_running_server(client: TestClient, uid: str) -> str:
    srv = create_server(client, name="hers")
    with Session(get_engine()) as s:
        row = s.get(Server, srv["id"])
        row.owner_id = uid
        row.enabled = True
        s.add(row)
        s.commit()
    return srv["id"]


def _session_for(uid: str) -> None:
    from datetime import timedelta

    from app.db.models import utcnow

    with Session(get_engine()) as s:
        s.add(SignInSession(id=new_id(), token_hash=hash_token(new_token()), user_id=uid,
                            expires_at=utcnow() + timedelta(hours=1)))
        s.commit()


def _create(client: TestClient, headers: dict, body: dict):
    return client.post("/scim/v2/Users", json=body, headers=headers)


# ---- Users ---------------------------------------------------------------------


def test_entra_creates_a_member_with_local_runners_off(idp):
    with TestClient(app) as client:
        r = _create(client, _scim(), fx.entra_create("ada@example.com"))
        assert r.status_code == 201, r.text
        assert r.headers["content-type"].startswith(SCIM)
        body = r.json()
        assert body["schemas"] == [fx.USER]
        assert body["userName"] == "ada@example.com"
        assert body["displayName"] == "Ada Lovelace"
        assert body["externalId"] == "0a21f0f2-8d2a-4f8e-bf98-7363c4aed4ef"
        assert body["active"] is True
        assert body["meta"]["resourceType"] == "User"
        assert r.headers["location"].endswith(f"/scim/v2/Users/{body['id']}")
        row = _user_row(body["id"])
        assert (row.role, row.local_runners, row.active) == ("member", False, True)
        got = client.get(f"/scim/v2/Users/{body['id']}", headers=_scim())
        assert got.status_code == 200 and got.json()["userName"] == "ada@example.com"


def test_okta_creates_with_plain_json(idp):
    with TestClient(app) as client:
        headers = {**_scim(), "content-type": "application/json"}
        r = _create(client, headers, fx.okta_create("grace@example.com"))
        assert r.status_code == 201, r.text
        assert r.json()["externalId"] == "00ujl29u0le5T6Aj10h7"


def test_a_duplicate_user_name_is_409_uniqueness(idp):
    with TestClient(app) as client:
        headers = _scim()
        assert _create(client, headers, fx.okta_create("ada@example.com")).status_code == 201
        r = _create(client, headers, fx.entra_create("ADA@example.com"))
        assert r.status_code == 409
        body = r.json()
        assert body["schemas"] == [ERROR]
        assert body["status"] == "409" and body["scimType"] == "uniqueness"


def test_filter_by_user_name_is_case_insensitive_and_pages(idp):
    with TestClient(app) as client:
        headers = _scim()
        for name in ("ada@example.com", "grace@example.com", "linus@example.com"):
            assert _create(client, headers, fx.okta_create(name)).status_code == 201
        r = client.get(
            "/scim/v2/Users", params={"filter": 'userName eq "Ada@Example.com"',
                                      "startIndex": 1, "count": 100}, headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schemas"] == [LIST]
        assert body["totalResults"] == 1
        assert [u["userName"] for u in body["Resources"]] == ["ada@example.com"]

        none = client.get("/scim/v2/Users", params={"filter": 'userName eq "nobody"'},
                          headers=headers).json()
        assert none["totalResults"] == 0 and none["Resources"] == []

        page = client.get("/scim/v2/Users", params={"startIndex": 2, "count": 1},
                          headers=headers).json()
        assert page["totalResults"] == 3
        assert page["startIndex"] == 2 and page["itemsPerPage"] == 1
        assert [u["userName"] for u in page["Resources"]] == ["grace@example.com"]

        bad = client.get("/scim/v2/Users", params={"filter": 'emails co "x"'}, headers=headers)
        assert bad.status_code == 400 and bad.json()["scimType"] == "invalidFilter"


def test_okta_replaces_and_entra_patches_attributes(idp):
    with TestClient(app) as client:
        headers = _scim()
        uid = _create(client, headers, fx.okta_create("ada@example.com")).json()["id"]
        r = client.put(f"/scim/v2/Users/{uid}",
                       json=fx.okta_replace(uid, "ada.king@example.com", "Ada King"),
                       headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["userName"] == "ada.king@example.com"
        assert _user_row(uid).name == "ada.king@example.com"

        r = client.patch(f"/scim/v2/Users/{uid}", json=fx.entra_replace_attributes(),
                         headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["displayName"] == "Ada King" and r.json()["externalId"] == "entra-2"


def _deactivation_case(client: TestClient, body: dict) -> None:
    uid = _user("ada@example.com")
    server_id = _owns_running_server(client, uid)
    _token(uid, "control")
    _token(uid, server_id)
    _session_for(uid)
    r = client.patch(f"/scim/v2/Users/{uid}", json=body, headers=_scim())
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    assert _user_row(uid).active is False
    assert _tokens_of(uid) == 0
    assert _sessions_of(uid) == 0
    with Session(get_engine()) as s:
        server = s.get(Server, server_id)
        assert (server.owner_id, server.enabled) == (uid, True)


def test_entra_string_false_deactivates(idp):
    with TestClient(app) as client:
        _deactivation_case(client, fx.entra_active("False"))


def test_okta_boolean_false_deactivates(idp):
    with TestClient(app) as client:
        _deactivation_case(client, fx.okta_active(False))


def test_patching_active_back_to_true_reactivates(idp):
    with TestClient(app) as client:
        headers = _scim()
        uid = _create(client, headers, fx.entra_create("ada@example.com")).json()["id"]
        assert client.patch(f"/scim/v2/Users/{uid}", json=fx.entra_active("False"),
                            headers=headers).json()["active"] is False
        r = client.patch(f"/scim/v2/Users/{uid}", json=fx.entra_active("True"), headers=headers)
        assert r.status_code == 200 and r.json()["active"] is True
        assert _user_row(uid).active is True
        r = client.patch(f"/scim/v2/Users/{uid}", json=fx.okta_active(False), headers=headers)
        assert r.json()["active"] is False
        r = client.patch(f"/scim/v2/Users/{uid}", json=fx.okta_active(True), headers=headers)
        assert r.json()["active"] is True


def test_delete_of_an_owner_deactivates_instead(idp):
    with TestClient(app) as client:
        uid = _user("ada@example.com")
        server_id = _owns_running_server(client, uid)
        _token(uid, "control")
        r = client.delete(f"/scim/v2/Users/{uid}", headers=_scim())
        assert r.status_code == 204, r.text
        row = _user_row(uid)
        assert row is not None and row.active is False
        assert _tokens_of(uid) == 0
        with Session(get_engine()) as s:
            assert s.get(Server, server_id).owner_id == uid


def test_delete_of_a_non_owner_deletes(idp):
    with TestClient(app) as client:
        headers = _scim()
        uid = _create(client, headers, fx.okta_create("ada@example.com")).json()["id"]
        _token(uid, "control")
        assert client.delete(f"/scim/v2/Users/{uid}", headers=headers).status_code == 204
        assert _user_row(uid) is None and _tokens_of(uid) == 0
        r = client.get(f"/scim/v2/Users/{uid}", headers=headers)
        assert r.status_code == 404 and r.json()["schemas"] == [ERROR]


def test_the_last_admin_can_not_be_deactivated_or_deleted(idp):
    with TestClient(app) as client:
        uid = _user("root@example.com", role="admin")
        _token(uid, "control")  # the only admin login
        headers = _scim()
        for r in (
            client.patch(f"/scim/v2/Users/{uid}", json=fx.entra_active("False"), headers=headers),
            client.patch(f"/scim/v2/Users/{uid}", json=fx.okta_active(False), headers=headers),
            client.delete(f"/scim/v2/Users/{uid}", headers=headers),
        ):
            assert r.status_code == 409, r.text
            assert r.json()["schemas"] == [ERROR] and "last admin" in r.json()["detail"]
        row = _user_row(uid)
        assert row is not None and row.active is True and _tokens_of(uid) == 1


# ---- discovery -----------------------------------------------------------------


def test_service_provider_config_schemas_and_resource_types_parse(idp):
    with TestClient(app) as client:
        headers = _scim()
        spc = client.get("/scim/v2/ServiceProviderConfig", headers=headers)
        assert spc.status_code == 200 and spc.headers["content-type"].startswith(SCIM)
        body = spc.json()
        assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"]
        assert body["patch"]["supported"] is True and body["filter"]["supported"] is True
        assert body["bulk"]["supported"] is False
        assert body["authenticationSchemes"][0]["type"] == "oauthbearertoken"

        schemas = client.get("/scim/v2/Schemas", headers=headers).json()
        assert [s["id"] for s in schemas["Resources"]] == [fx.USER]
        names = {a["name"] for a in schemas["Resources"][0]["attributes"]}
        assert {"userName", "displayName", "externalId", "active"} <= names

        types = client.get("/scim/v2/ResourceTypes", headers=headers).json()
        assert [(t["name"], t["endpoint"]) for t in types["Resources"]] == [("User", "/Users")]


def test_groups_are_not_served(idp):
    with TestClient(app) as client:
        r = client.get("/scim/v2/Groups", headers=_scim())
        assert r.status_code == 404
        assert r.json()["schemas"] == [ERROR] and r.json()["status"] == "404"


# ---- the scim token ------------------------------------------------------------


def test_scim_needs_a_scim_token(idp, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "break-glass-admin-token")
    with TestClient(app) as client:
        missing = client.get("/scim/v2/Users", headers=LOOPBACK)
        assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
        assert missing.json()["schemas"] == [ERROR]
        assert client.get("/scim/v2/Users", headers=_headers("nope")).status_code == 401
        for raw in (mint_token("control"), mint_token("all"), "break-glass-admin-token"):
            r = client.get("/scim/v2/Users", headers=_headers(raw))
            assert r.status_code == 403, r.text
            assert r.json()["schemas"] == [ERROR]


def test_scim_is_behind_the_host_guard(idp):
    with TestClient(app) as client:
        r = client.get("/scim/v2/Users", headers={**_scim(), "origin": "https://evil.example"})
        assert r.status_code == 403 and "schemas" not in r.json()


def test_a_scim_token_opens_nothing_else(idp):
    with TestClient(app) as client:
        admin = _enforce()
        with Session(get_engine()) as s:
            service.ensure_builtin(s)
            runtime_settings.write(s, {"default_auth_provider": "bearer"})
        srv = client.post(
            "/api/servers", json={"name": "px", "command": "echo", "auth_provider": "bearer"},
            headers=admin,
        ).json()
        scim = {**_scim(), "accept": "application/json, text/event-stream"}
        assert client.get("/api/servers", headers=scim).status_code in (401, 403)
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        for path in (f"/s/{srv['slug']}/mcp", "/api/mcp"):
            assert client.post(path, json=body, headers=scim).status_code in (401, 403), path


def test_an_admin_mints_a_scim_token_and_a_member_can_not(idp):
    with TestClient(app) as client:
        admin = _enforce()
        r = client.post("/api/tokens", json={"name": "Entra", "scope": "scim"}, headers=admin)
        assert r.status_code == 201, r.text
        assert r.json()["scope"] == "scim"
        assert client.get("/scim/v2/Users", headers=_headers(r.json()["token"])).status_code == 200

        member = _user("bob@example.com")
        login = {**LOOPBACK, "authorization": f"Bearer {_token(member, 'control')}"}
        r = client.post("/api/tokens", json={"name": "mine", "scope": "scim"}, headers=login)
        assert r.status_code == 403, r.text
        with Session(get_engine()) as s:
            assert [t.scope for t in repo.list_tokens(s) if t.user_id == member] == ["control"]


# ---- linking -------------------------------------------------------------------


def test_a_scim_created_user_links_at_first_sign_in(idp):
    idp.claims = {"email": "ada@example.com"}
    with TestClient(app) as client:
        _enforce()
        uid = _create(client, _scim(), fx.entra_create("ada@example.com")).json()["id"]
        assert _user_row(uid).oidc_issuer is None
        assert _signed_in(_sign_in(client, idp))
        row = _user_row(uid)
        assert row.oidc_issuer is not None and row.oidc_subject is not None
        with Session(get_engine()) as s:
            assert len(repo.users_named(s, "ada@example.com")) == 1


def test_the_password_okta_sends_is_ignored_and_never_stored(idp):
    body = fx.okta_create("ada@example.com")
    assert body["password"] == "<okta-generated>"
    with TestClient(app) as client:
        r = _create(client, _scim(), body)
        assert r.status_code == 201, r.text
        assert "password" not in r.json() and body["password"] not in r.text
        stored = repr(_user_row(r.json()["id"]).model_dump())
        assert body["password"] not in stored


def test_a_scim_token_revoked_while_a_write_waits_on_the_lock_is_refused(idp, monkeypatch):
    import threading
    from contextlib import contextmanager

    from app.api import scim as scim_api

    real = service.config_write_lock
    waiting, go = threading.Event(), threading.Event()

    @contextmanager
    def gated():
        waiting.set()  # the write has passed require_scim and now wants the lock
        assert go.wait(30)
        with real():
            yield

    with TestClient(app) as client:
        raw = mint_token("scim")
        uid = _create(client, _headers(raw), fx.okta_create("ada@example.com")).json()["id"]
        monkeypatch.setattr(scim_api.service, "config_write_lock", gated)
        result = {}
        patch = threading.Thread(target=lambda: result.update(r=client.patch(
            f"/scim/v2/Users/{uid}", json=fx.okta_active(False), headers=_headers(raw))))
        patch.start()
        assert waiting.wait(30)
        with Session(get_engine()) as s:
            s.delete(repo.get_token_by_hash(s, hash_token(raw)))
            s.commit()
        go.set()
        patch.join(30)
        r = result["r"]
        assert r.status_code == 401, r.text
        assert r.json()["schemas"] == [ERROR]
        assert _user_row(uid).active is True


def test_the_filter_value_is_a_json_string(idp):
    with TestClient(app) as client:
        headers = _scim()
        for name in ('ada "the countess"@example.com', "josé@example.com"):
            assert _create(client, headers, fx.okta_create(name)).status_code == 201
        for filter_, want in (
            (r'userName eq "ada \"the countess\"@example.com"', 'ada "the countess"@example.com'),
            (r'userName eq "josé@example.com"', "josé@example.com"),
        ):
            body = client.get("/scim/v2/Users", params={"filter": filter_}, headers=headers).json()
            assert [u["userName"] for u in body["Resources"]] == [want], filter_
        for bad in (r'userName eq "ada\x"', 'userName eq "ada'):
            r = client.get("/scim/v2/Users", params={"filter": bad}, headers=headers)
            assert r.status_code == 400 and r.json()["scimType"] == "invalidFilter", bad


# ---- who SCIM may manage ---------------------------------------------------------

ATTACKER = "mallory@example.com"


def _rename(name: str) -> dict:
    return {"schemas": [fx.PATCH_OP],
            "Operations": [{"op": "replace", "path": "userName", "value": name}]}


def _assert_mutability(r) -> None:
    assert r.status_code == 400, r.text
    assert r.json()["schemas"] == [ERROR] and r.json()["scimType"] == "mutability"


def test_scim_can_not_rename_an_admin_it_did_not_create_into_an_sso_takeover(idp):
    root = _user("root@example.com", role="admin")
    idp.claims = {"email": ATTACKER}
    with TestClient(app) as client:
        _enforce()
        headers = _scim()
        _assert_mutability(client.patch(f"/scim/v2/Users/{root}", json=_rename(ATTACKER),
                                        headers=headers))
        _assert_mutability(client.put(f"/scim/v2/Users/{root}",
                                      json=fx.okta_replace(root, ATTACKER, "Mallory"),
                                      headers=headers))
        assert _user_row(root).name == "root@example.com"
        assert not _signed_in(_sign_in(client, idp))
        assert _user_row(root).oidc_subject is None


def test_scim_can_not_rename_an_admin_into_a_cloudflare_access_takeover(idp, monkeypatch):
    from app.auth import cf_access

    root = _user("root@example.com", role="admin")
    monkeypatch.setattr(cf_access, "email", lambda request: ATTACKER)
    with TestClient(app) as client:
        _assert_mutability(client.patch(f"/scim/v2/Users/{root}", json=_rename(ATTACKER),
                                        headers=_scim()))
        with Session(get_engine()) as s:
            assert cf_access.user(s, None) is None


def test_scim_changes_only_active_on_a_user_it_did_not_create(idp):
    uid = _user("bob@example.com")
    with TestClient(app) as client:
        headers = _scim()
        for body in (fx.entra_replace_attributes(), _rename("robert@example.com")):
            _assert_mutability(client.patch(f"/scim/v2/Users/{uid}", json=body, headers=headers))
        r = client.patch(f"/scim/v2/Users/{uid}", json=fx.entra_active("False"), headers=headers)
        assert r.status_code == 200 and r.json()["active"] is False
        # A replace that resends the same name only changes active.
        r = client.put(f"/scim/v2/Users/{uid}", json={"schemas": [fx.USER],
                       "userName": "bob@example.com", "active": True}, headers=headers)
        assert r.status_code == 200, r.text
        row = _user_row(uid)
        assert (row.name, row.active) == ("bob@example.com", True)


def test_scim_renames_its_own_member_and_keeps_the_link(idp):
    with TestClient(app) as client:
        headers = _scim()
        uid = _create(client, headers, fx.okta_create("ada@example.com")).json()["id"]
        with Session(get_engine()) as s:
            row = s.get(User, uid)
            row.oidc_issuer, row.oidc_subject = "https://idp.example", "sub-1"
            s.add(row)
            s.commit()
        r = client.patch(f"/scim/v2/Users/{uid}", json=_rename("ada.king@example.com"),
                         headers=headers)
        assert r.status_code == 200, r.text
        row = _user_row(uid)
        assert row.name == "ada.king@example.com"
        assert (row.oidc_issuer, row.oidc_subject) == ("https://idp.example", "sub-1")
        _user("grace@example.com")
        r = client.patch(f"/scim/v2/Users/{uid}", json=_rename("Grace@example.com"),
                         headers=headers)
        assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"


def test_scim_can_not_rename_its_own_user_once_an_admin_promotes_them(idp):
    with TestClient(app) as client:
        admin = _enforce()
        headers = _scim()
        uid = _create(client, headers, fx.okta_create("ada@example.com")).json()["id"]
        assert client.patch(f"/api/users/{uid}", json={"role": "admin"},
                            headers=admin).status_code == 200
        _assert_mutability(client.patch(f"/scim/v2/Users/{uid}", json=_rename(ATTACKER),
                                        headers=headers))
        assert _user_row(uid).name == "ada@example.com"
