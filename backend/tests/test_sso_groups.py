"""Provider groups decide who signs in, who is Admin and who runs local runners (#270):
the groups claim in its provider shapes, the allowed-groups and allowed-domains gates,
role and local-runner mapping at sign-in, and the lock on managed fields."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from conftest import LOOPBACK

# idp is the fixture; the rest are the sign-in helpers.
from test_sso_sign_in import (
    _callback,
    _enforce,
    _linked_issuer,
    _reason,
    _sessions,
    _settings,
    _sign_in,
    _signed_in,
    _start,
    _user,
    idp,
)

from app.db import get_engine, repo
from app.db.models import Token
from app.main import app
from app.registry import settings as runtime_settings
from app.util import hash_token, new_id, new_token


def _role(uid: str) -> tuple[str, bool]:
    with Session(get_engine()) as s:
        user = repo.get_user(s, uid)
        return user.role, bool(user.local_runners)


def _ada(role: str = "member") -> str:
    return _user("ada@example.com", role=role)


# ---- reading the groups claim -------------------------------------------------------


@pytest.mark.parametrize(
    ("claim_name", "claims"),
    [
        ("groups", {"groups": ["staff", 7, "admins"]}),
        ("groups", {"groups": "admins"}),  # a single string is one group
        ("https://acme.example/groups", {"https://acme.example/groups": ["admins"]}),  # Auth0
        ("realm_access.roles", {"realm_access": {"roles": ["admins"]}}),  # Keycloak
        (
            "urn:zitadel:iam:org:project:roles",  # Zitadel: an object, its keys are the roles
            {"urn:zitadel:iam:org:project:roles": {"admins": {"123": "acme.example"}}},
        ),
    ],
    ids=["list", "single_string", "literal_url_name", "dotted_path", "object_keys"],
)
def test_the_groups_claim_is_read_in_each_provider_shape(idp, claim_name, claims):
    uid = _ada()
    idp.claims = {"email": "ada@example.com", **claims}
    _settings(oidc_groups_claim=claim_name, oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _role(uid)[0] == "admin"


def test_groups_only_in_userinfo_count(idp):  # GitLab and Authelia
    uid = _ada()
    idp.claims = {"email": "ada@example.com", "groups_direct": ["other"]}
    idp.userinfo = {"sub": "sub-1", "groups": ["admins"]}
    _settings(oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _role(uid)[0] == "admin"


@pytest.mark.parametrize(
    "claims",
    [
        {"_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {"endpoint": "x"}}},
        {"_claim_names": {"roles": "src1"}},
    ],
    ids=["groups", "configured_name"],
)
def test_an_entra_groups_overage_refuses_the_sign_in(idp, claims):
    _ada()
    idp.claims = {"email": "ada@example.com", **claims}
    _settings(oidc_groups_claim="roles", oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "groups_overage"


def test_an_overage_without_group_lists_still_signs_in(idp):
    _ada()
    idp.claims = {"email": "ada@example.com", "_claim_names": {"groups": "src1"}}
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


# ---- the gates ----------------------------------------------------------------------


def test_someone_in_none_of_the_allowed_groups_is_not_allowed(idp):
    _ada()
    idp.claims = {"email": "ada@example.com", "groups": ["contractors"]}
    _settings(oidc_allowed_groups=["staff"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "not_allowed"
        idp.claims["groups"] = ["contractors", "staff"]
        assert _signed_in(_sign_in(client, idp))


def test_no_groups_claim_with_allowed_groups_is_not_allowed(idp):
    _ada()
    idp.claims = {"email": "ada@example.com"}
    _settings(oidc_allowed_groups=["staff"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "not_allowed"


def test_an_unlisted_email_domain_is_refused(idp):
    _ada()
    idp.claims = {"email": "ada@example.com", "email_verified": True}
    _settings(oidc_allowed_domains=["acme.example"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "domain_not_allowed"
        _settings(oidc_allowed_domains=["Example.COM"])
        assert _signed_in(_sign_in(client, idp))


def test_an_unverified_emails_domain_does_not_count(idp):
    _user("ada")
    idp.claims = {"preferred_username": "ada", "email": "ada@example.com", "email_verified": False}
    _settings(oidc_identity_claim="preferred_username", oidc_allowed_domains=["example.com"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "domain_not_allowed"


@pytest.mark.parametrize("verified", [None, 1, "yes"], ids=["absent", "number", "word"])
def test_an_email_domain_without_an_explicit_true_verification_does_not_count(idp, verified):
    # Entra's email is editable and carries no email_verified: no evidence, no domain.
    _user("ada")
    idp.claims = {"preferred_username": "ada", "email": "ada@example.com"}
    if verified is not None:
        idp.claims["email_verified"] = verified
    _settings(oidc_identity_claim="preferred_username", oidc_allowed_domains=["example.com"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "domain_not_allowed"


@pytest.mark.parametrize("verified", [True, "true", "TRUE"])
def test_an_explicitly_verified_emails_domain_counts(idp, verified):
    _user("ada")
    idp.claims = {"preferred_username": "ada", "email": "ada@example.com", "email_verified": verified}
    _settings(oidc_identity_claim="preferred_username", oidc_allowed_domains=["example.com"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


def test_googles_hd_passes_a_listed_domain(idp):
    _user("ada@gmail-alias.example")
    idp.claims = {"email": "ada@gmail-alias.example", "hd": "acme.example"}
    _settings(oidc_allowed_domains=["acme.example"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


# ---- mapping at sign-in -------------------------------------------------------------


def _admin_only_token(uid: str) -> str:
    raw = new_token()
    with Session(get_engine()) as s:
        repo.create_token(s, Token(
            id=new_id(), name="all", token_hash=hash_token(raw), prefix=raw[:12], scope="all",
            user_id=uid,
        ))
    return raw


def test_sign_in_promotes_and_grants_local_runners(idp):
    uid = _ada()
    idp.claims = {"email": "ada@example.com", "groups": ["admins", "builders"]}
    _settings(oidc_admin_groups=["admins"], oidc_local_runner_groups=["builders"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _role(uid) == ("admin", True)


def test_removal_from_the_admin_group_demotes_and_sweeps_admin_only_tokens(idp):
    uid = _ada(role="admin")
    _admin_only_token(uid)
    idp.claims = {"email": "ada@example.com", "groups": ["staff"]}
    _settings(oidc_admin_groups=["admins"], oidc_local_runner_groups=["builders"])
    with TestClient(app) as client:
        admin = _enforce()
        assert _signed_in(_sign_in(client, idp))
        listed = {u["id"]: u for u in client.get("/api/users", headers=admin).json()}
    assert listed[uid]["role"] == "member" and listed[uid]["local_runners"] is False
    assert listed[uid]["tokens_count"] == 0


def test_an_empty_list_leaves_the_field_manual(idp):
    uid = _ada(role="admin")
    idp.claims = {"email": "ada@example.com", "groups": ["builders"]}
    _settings(oidc_local_runner_groups=["builders"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _role(uid) == ("admin", True)


def _login_token(uid: str) -> None:
    with Session(get_engine()) as s:
        repo.create_token(s, Token(
            id=new_id(), name="login", token_hash=hash_token(new_token()), prefix="x",
            scope="control", user_id=uid,
        ))


def test_the_last_admin_removed_from_the_admin_groups_is_refused(idp, caplog):
    uid = _ada(role="admin")
    _login_token(uid)  # her login token is the only admin credential: kept for recovery
    idp.claims = {"email": "ada@example.com", "groups": ["builders"]}
    _settings(oidc_admin_groups=["admins"])
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="app"):
        assert _reason(_sign_in(client, idp)) == "last_admin_demoted"
    assert _role(uid)[0] == "admin"
    assert _sessions() == 0
    assert _linked_issuer("ada@example.com") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "last admin" in warnings[0].getMessage()


def test_with_a_second_admin_login_the_demotion_applies(idp):
    uid = _ada(role="admin")
    _login_token(uid)
    _login_token(_user("grace", role="admin"))
    idp.claims = {"email": "ada@example.com", "groups": ["builders"]}
    _settings(oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _role(uid)[0] == "member"
    assert _sessions() == 1


def test_a_groups_claim_change_while_the_provider_answers_promotes_no_one(idp):
    uid = _ada()
    idp.claims = {"email": "ada@example.com", "groups": ["admins"], "app_roles": ["staff"]}
    _settings(oidc_admin_groups=["admins"])
    answer = idp._token

    def slow_token(request):  # the admin points the mapping at another claim meanwhile
        _settings(oidc_groups_claim="app_roles")
        return answer(request)

    idp._token = slow_token
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "expired"
    assert _role(uid)[0] == "member"
    assert _sessions() == 0


def test_a_provider_change_while_the_provider_answers_writes_no_groups_or_link(idp):
    uid = _ada()
    idp.claims = {"email": "ada@example.com", "groups": ["admins"]}
    _settings(oidc_admin_groups=["admins"])
    answer = idp._token

    def slow_token(request):  # an admin moves SSO to another client while this is pending
        _settings(oidc_client_id="another")
        return answer(request)

    idp._token = slow_token
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "expired"
    assert _role(uid)[0] == "member"
    assert _linked_issuer("ada@example.com") is None
    assert _sessions() == 0


# ---- the lock on managed fields -------------------------------------------------------


def test_a_managed_field_is_locked_against_patch(idp):
    uid = _ada()
    _settings(oidc_admin_groups=["admins"])
    with TestClient(app) as client:
        admin = _enforce()
        r = client.patch(f"/api/users/{uid}", json={"role": "admin"}, headers=admin)
        assert r.status_code == 409
        assert "oidc_admin_groups" in r.json()["detail"]
        # the unmanaged field still saves
        r = client.patch(f"/api/users/{uid}", json={"local_runners": True}, headers=admin)
        assert r.status_code == 200
        assert r.json()["managed"] == {"role": True, "local_runners": False}
        _settings(oidc_local_runner_groups=["builders"])
        r = client.patch(f"/api/users/{uid}", json={"local_runners": False}, headers=admin)
        assert r.status_code == 409
        assert "oidc_local_runner_groups" in r.json()["detail"]


# ---- the groups scope ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("advertised", "mapped", "expected"),
    [(True, True, True), (False, True, False), (True, False, False)],
    ids=["advertised_and_needed", "not_advertised", "not_needed"],
)
def test_the_groups_scope_is_requested_only_when_advertised_and_needed(
    idp, advertised, mapped, expected
):
    if advertised:
        idp.scopes_supported = ["openid", "email", "profile", "groups"]
    if mapped:
        _settings(oidc_allowed_groups=["staff"])
    with TestClient(app) as client:
        idp.authorize(_start(client).headers["location"])
    assert ("groups" in idp.last_authorize["scope"].split()) is expected


# ---- settings -----------------------------------------------------------------------


def test_group_settings_are_validated_and_deduplicated(idp):
    with TestClient(app) as client:
        r = client.patch("/api/settings", headers=LOOPBACK, json={
            "oidc_admin_groups": [" admins ", "admins", "ops"],
            "oidc_allowed_domains": ["Example.COM", "example.com"],
            "oidc_groups_claim": " realm_access.roles ",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["oidc_admin_groups"] == ["admins", "ops"]
        assert body["oidc_allowed_domains"] == ["example.com"]
        assert body["oidc_groups_claim"] == "realm_access.roles"
        for bad in ({"oidc_allowed_domains": ["ada@example.com"]},
                    {"oidc_allowed_groups": [""]}, {"oidc_groups_claim": ""}):
            assert client.patch("/api/settings", headers=LOOPBACK, json=bad).status_code == 400


def test_group_settings_seed_once_from_the_env(idp, monkeypatch):
    from app import main

    env = SimpleNamespace(
        oidc_groups_claim="roles", oidc_allowed_groups="", oidc_admin_groups="admins, ops",
        oidc_local_runner_groups="", oidc_allowed_domains="Example.com",
    )
    monkeypatch.setattr(main, "get_settings", lambda: env)
    main._bootstrap_oidc_groups()
    with Session(get_engine()) as s:
        values = runtime_settings.read_all(s)
    assert values["oidc_admin_groups"] == ["admins", "ops"]
    assert values["oidc_allowed_domains"] == ["example.com"]
    assert values["oidc_groups_claim"] == "roles"


def test_a_login_with_long_group_and_tenant_lists_fits_its_flow_cookie(idp):
    import uuid

    uid = _ada()
    groups = [str(uuid.uuid4()) for _ in range(300)]
    idp.claims = {"email": "ada@example.com", "groups": groups[:1]}
    _settings(
        oidc_allowed_groups=groups,
        oidc_admin_groups=groups[:1],
        oidc_allowed_tenants=[str(uuid.uuid4()) for _ in range(100)],
    )
    with TestClient(app) as client:
        start = _start(client)
        flow = next(h for h in start.headers.get_list("set-cookie") if h.startswith("mcpe_oidc_"))
        assert len(flow.split(";", 1)[0].encode()) < 4096
        assert _signed_in(_callback(client, idp.authorize(start.headers["location"])))
    assert _role(uid)[0] == "admin"


# ---- email and email_verified come from one response (#291) -------------------------


def test_userinfo_cannot_verify_a_different_id_token_email(idp):
    # The ID token's email carries no email_verified; userinfo verifies another address.
    _ada()
    idp.claims = {"email": "ada@example.com"}
    idp.userinfo = {"sub": "sub-1", "email": "ada@other.example", "email_verified": True}
    _settings(oidc_allowed_domains=["example.com", "other.example"])
    with TestClient(app) as client:
        assert _reason(_sign_in(client, idp)) == "domain_not_allowed"


@pytest.mark.parametrize("userinfo_verified", [True, False])
def test_matching_reads_the_id_token_emails_own_verification(idp, userinfo_verified):
    # Absent counts as verified for matching, whatever userinfo says of another address.
    _ada()
    idp.claims = {"email": "ada@example.com"}
    idp.userinfo = {
        "sub": "sub-1", "email": "ada@other.example", "email_verified": userinfo_verified,
    }
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
    assert _linked_issuer("ada@example.com") is not None


def test_an_id_token_email_verified_without_an_email_does_not_judge_userinfos(idp):
    _ada()
    idp.claims = {"email_verified": False}
    idp.userinfo = {"sub": "sub-1", "email": "ada@example.com", "email_verified": True}
    _settings(oidc_allowed_domains=["example.com"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


def test_an_email_and_verification_only_in_userinfo_count(idp):  # Authelia
    _ada()
    idp.userinfo = {"sub": "sub-1", "email": "ada@example.com", "email_verified": True}
    _settings(oidc_allowed_domains=["example.com"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))


def test_userinfo_verifies_the_same_email_the_id_token_carries(idp):
    _ada()
    idp.claims = {"email": "ada@example.com"}
    idp.userinfo = {"sub": "sub-1", "email": "ada@example.com", "email_verified": True}
    _settings(oidc_allowed_domains=["example.com"])
    with TestClient(app) as client:
        assert _signed_in(_sign_in(client, idp))
