"""Secret env (app.sealed): chosen per key, encrypted at rest, never returned, and not
carried through a change that could read it back out."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlmodel import Session

from conftest import LOOPBACK

from app import sealed
from app.db import get_engine, repo
from app.main import app
from app.runners import build_spec

HIDDEN = sealed.HIDDEN


def _row(server_id: str):
    with Session(get_engine()) as session:
        return repo.get_server(session, server_id)


def _detail(c, server_id):
    return c.get(f"/api/servers/{server_id}", headers=LOOPBACK).json()


def _patch(c, server_id, **changes):
    return c.patch(f"/api/servers/{server_id}", json=changes, headers=LOOPBACK)


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


def _create(c, **fields):
    r = c.post(
        "/api/servers",
        json={"name": "Secret", "runner": "command", "command": "/bin/true", **fields},
        headers=LOOPBACK,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def server(c):
    server_id = _create(c, env={"API_KEY": "s3cret", "REGION": "eu"}, secret_env=["API_KEY"])
    yield server_id
    c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_secret_is_hidden_and_sealed_plain_stays_readable(c, server):
    detail = _detail(c, server)
    assert detail["env"] == {"API_KEY": HIDDEN, "REGION": "eu"}
    assert detail["secret_env"] == ["API_KEY"]
    row = _row(server)
    assert sealed.is_sealed(row.env["API_KEY"]) and row.env["REGION"] == "eu"
    assert "s3cret" not in str(row.env)
    assert build_spec(row).env == {"API_KEY": "s3cret", "REGION": "eu"}


def test_resending_hidden_keeps_value_and_hash(c, server):
    before = _row(server)
    r = _patch(c, server, name="Renamed", command="/bin/true",
               env={"API_KEY": HIDDEN, "REGION": "eu"}, secret_env=["API_KEY"])
    assert r.status_code == 200, r.text
    after = _row(server)
    assert after.env == before.env and after.config_hash == before.config_hash


def test_omitting_secret_env_keeps_each_keys_secrecy(c, server):
    assert _patch(c, server, env={"API_KEY": HIDDEN, "REGION": "us", "NEW": "x"}).status_code == 200
    assert _detail(c, server)["env"] == {"API_KEY": HIDDEN, "REGION": "us", "NEW": "x"}


def test_new_value_replaces_and_missing_key_drops(c, server):
    assert _patch(c, server, env={"API_KEY": "rotated"}).status_code == 200
    assert build_spec(_row(server)).env == {"API_KEY": "rotated"}


def test_retarget_needs_secret_values_again(c, server):
    r = _patch(c, server, command="/usr/bin/env", env={"API_KEY": HIDDEN, "REGION": "eu"})
    assert r.status_code == 400
    assert "API_KEY" in r.json()["detail"] and "REGION" not in r.json()["detail"]
    # Not sending env at all is the same as keeping every secret value.
    assert _patch(c, server, args=["--dump"]).status_code == 400
    assert _row(server).command == "/bin/true"
    r = _patch(c, server, command="/usr/bin/env", env={"API_KEY": "s3cret", "REGION": "eu"})
    assert r.status_code == 200, r.text
    assert build_spec(_row(server)).env == {"API_KEY": "s3cret", "REGION": "eu"}


def test_making_a_secret_visible_needs_it_again(c, server):
    r = _patch(c, server, secret_env=[])
    assert r.status_code == 400
    assert "visible" in r.json()["detail"]
    r = _patch(c, server, secret_env=[], env={"API_KEY": "s3cret", "REGION": "eu"})
    assert r.status_code == 200, r.text
    assert _detail(c, server)["env"] == {"API_KEY": "s3cret", "REGION": "eu"}


def test_marking_an_existing_key_secret_seals_it(c, server):
    assert _patch(c, server, secret_env=["API_KEY", "REGION"]).status_code == 200
    assert _detail(c, server)["env"] == {"API_KEY": HIDDEN, "REGION": HIDDEN}
    assert sealed.is_sealed(_row(server).env["REGION"])


def test_remote_headers_can_be_secret(c):
    server_id = _create(
        c,
        runner="remote",
        command="https://up.example/mcp",
        args=["streamable-http"],
        env={"Authorization": "Bearer t0ken", "X-Team": "ops"},
        secret_env=["Authorization"],
    )
    try:
        assert _detail(c, server_id)["env"] == {"Authorization": HIDDEN, "X-Team": "ops"}
        assert build_spec(_row(server_id)).env["Authorization"] == "Bearer t0ken"
        # Pointing the headers at another host needs the secret again.
        r = _patch(c, server_id, command="https://evil.example/mcp")
        assert r.status_code == 400
    finally:
        c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"NEW": HIDDEN}, "no hidden value is stored"),
        ({"API_KEY": sealed.PREFIX + "x"}, "can't start with"),
    ],
)
def test_bad_values_are_refused(c, server, env, message):
    r = _patch(c, server, env=env)
    assert r.status_code == 400
    assert message in r.json()["detail"]


def test_clone_keeps_secret_keys(c, server):
    r = c.post(f"/api/servers/{server}/clone", json={}, headers=LOOPBACK)
    assert r.status_code == 201, r.text
    clone_id = r.json()["id"]
    try:
        clone = _row(clone_id)
        assert sealed.secret_keys(clone.env) == ["API_KEY"]
        assert clone.env["API_KEY"] != _row(server).env["API_KEY"]  # sealed afresh
        assert build_spec(clone).env == {"API_KEY": "s3cret", "REGION": "eu"}
    finally:
        c.delete(f"/api/servers/{clone_id}", headers=LOOPBACK)


def test_lost_key_names_the_values_to_reenter():
    foreign = sealed.PREFIX + Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    with pytest.raises(ValueError, match="TOKEN"):
        sealed.unseal_env({"TOKEN": foreign, "PLAIN": "ok"})
