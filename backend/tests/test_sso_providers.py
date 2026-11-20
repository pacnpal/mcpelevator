"""One end-to-end check per named identity provider (#284). Each preset in
``fake_idp.PRESETS`` plays that provider's documented behavior; the test configures SSO
the way README "Provider notes" tells an admin to, then signs a person in, maps their
groups (or domain), refreshes the session and logs out, checking what they can do at
each step."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from conftest import LOOPBACK
from fake_idp import CLIENT_ID, PRESETS, FakeIdP

# idp is the fixture; the rest are the sign-in and refresh helpers.
from test_sso_refresh import _age, _status
from test_sso_sign_in import _enforce, _session_headers, _sign_in, _signed_in, _user, idp

from app.main import app

__all__ = ["idp"]  # the fixture, used by name


def _swap(value: Any, old: str, new: str) -> Any:
    """``value`` with every string (and object key) equal to ``old`` turned into ``new``."""
    if isinstance(value, dict):
        return {_swap(k, old, new): _swap(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap(v, old, new) for v in value]
    return new if value == old else value


def _provider_swaps(idp: FakeIdP, old: str, new: str) -> None:
    """The provider's admin changes ``old`` to ``new`` wherever the provider keeps it."""
    idp.claims = _swap(idp.claims, old, new)
    idp.userinfo = _swap(idp.userinfo, old, new)
    idp.scope_claims = _swap(idp.scope_claims, old, new)
    idp.scope_userinfo = _swap(idp.scope_userinfo, old, new)


def _can(client: TestClient, headers: dict[str, str]) -> tuple[int, int]:
    """(GET /api/servers, GET /api/users): any signed-in user, then admins only."""
    return (client.get("/api/servers", headers=headers).status_code,
            client.get("/api/users", headers=headers).status_code)


@pytest.mark.parametrize("name", list(PRESETS))
def test_each_provider_signs_in_maps_refreshes_and_logs_out(idp, name):
    preset = PRESETS[name]
    idp.use(preset)
    claim = preset.settings.get("oidc_identity_claim", "email")
    _user({**(preset.userinfo or {}), **preset.id_token}[claim])
    with TestClient(app) as client:
        admin = _enforce()
        secret = {} if preset.public else {"oidc_client_secret": idp.client_secret}
        r = client.patch("/api/settings", headers=admin, json={
            "oidc_discovery_url": preset.issuer, "oidc_client_id": CLIENT_ID,
            **secret, **preset.settings,
        })
        assert r.status_code == 200, r.text

        # 1. Sign in, with the identity claim and client authentication the notes name.
        assert _signed_in(_sign_in(client, idp))
        headers = _session_headers(client)

        # 2. Groups map to Admin; a provider without groups is gated by domain instead
        # and stays a Member.
        if preset.admin_group is not None:
            assert _status(client)["user"]["role"] == "admin"
            assert _can(client, headers) == (200, 200)
        else:
            assert _status(client)["user"]["role"] == "member"
            assert _can(client, headers) == (200, 403)

        # 3. Refresh: twice as the provider stands, presenting the token it rotated
        # (or the same one, where it doesn't rotate), then once after the provider
        # removes the person from the admin group or the allowed domain.
        for _ in range(2):
            _age(soft=True)
            assert client.get("/api/servers", headers=headers).status_code == 401
            assert _status(client)["authenticated"] is True
        assert len(idp.refreshes) == 2
        assert (idp.refreshes[0] != idp.refreshes[1]) is preset.rotating
        if preset.admin_group is not None:
            _provider_swaps(idp, preset.admin_group, "staff")
            _age(soft=True)
            assert _status(client)["user"]["role"] == "member"
            assert _can(client, headers) == (200, 403)
        else:
            _provider_swaps(idp, "example.com", "elsewhere.example")
            _age(soft=True)
            assert _status(client)["authenticated"] is False
            assert _can(client, headers) == (401, 401)
            _provider_swaps(idp, "elsewhere.example", "example.com")
            assert _signed_in(_sign_in(client, idp))
            headers = _session_headers(client)

        # 4. Log out: at the provider when it lists an end_session_endpoint, here only
        # otherwise. Either way the session is over.
        r = client.post("/api/auth/logout", headers=LOOPBACK)
        assert r.status_code == 200
        url = r.json()["provider_logout_url"]
        if preset.end_session:
            parts = urlsplit(url)
            assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
                f"{preset.issuer.rstrip('/')}/logout"
            )
            query = parse_qs(parts.query)
            assert query["client_id"] == [CLIENT_ID] and "id_token_hint" in query
            assert query["post_logout_redirect_uri"][0].endswith("/login")
        else:
            assert url is None
        assert _status(client)["authenticated"] is False
        assert _can(client, headers) == (401, 401)
