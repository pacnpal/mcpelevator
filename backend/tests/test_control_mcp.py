"""The built-in mcpelevator MCP server: provisioning, the /api/mcp gate (global and
per-user switches, the bridge's internal header), tool calls running through the
caller's own /api authorization, and the edit guards on the built-in row."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from conftest import LOOPBACK, mint_token

from app.db import get_engine, repo
from app.db.models import BUILTIN_RUNNER, Server, Token, User
from app.groups import registry
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.runners.builtin import INTERNAL_HEADER, INTERNAL_SECRET
from app.supervisor.supervisor import Supervisor
from app.util import hash_token, new_id, new_token

MCP_HEADERS = {**LOOPBACK, "accept": "application/json, text/event-stream"}


async def _parked(self):
    import asyncio

    await asyncio.Event().wait()


@pytest.fixture
def client(monkeypatch):
    # Park the reconciler: the built-in bridge would dial a port TestClient never binds.
    monkeypatch.setattr(Supervisor, "run_forever", _parked)
    with TestClient(app) as c:
        with Session(get_engine()) as s:
            service.ensure_builtin(s)
        yield c
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "auto", "groups": {}})
        s.execute(delete(Token))
        s.execute(delete(User))
        s.execute(delete(Server))
        s.commit()


def _rpc(client, method: str, params: dict | None = None, headers: dict | None = None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = client.post("/api/mcp", json=body, headers={**MCP_HEADERS, **(headers or {})})
    if r.status_code != 200:
        return r.status_code, r.json()
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return 200, json.loads(line[5:])
    return 200, r.json()


def _call(client, tool: str, args: dict | None = None, headers: dict | None = None):
    status, msg = _rpc(client, "tools/call", {"name": tool, "arguments": args or {}}, headers)
    assert status == 200, msg
    result = msg["result"]
    text = result["content"][0]["text"] if result["content"] else "null"
    try:
        return result["isError"], json.loads(text)
    except ValueError:
        return result["isError"], text


def _builtin() -> Server:
    with Session(get_engine()) as s:
        return service.get_builtin(s)


def test_provisioned_once_and_listed(client):
    with Session(get_engine()) as s:
        assert service.ensure_builtin(s) is None  # idempotent
    b = _builtin()
    assert (b.runner, b.slug, b.enabled, b.auth_provider) == (BUILTIN_RUNNER, "mcpelevator", True, "bearer")

    status, msg = _rpc(client, "tools/list")
    assert status == 200
    names = {t["name"] for t in msg["result"]["tools"]}
    assert {"list_servers", "create_server", "put_group", "create_token", "update_settings"} <= names

    is_error, servers = _call(client, "list_servers")
    assert not is_error and [s["slug"] for s in servers["items"]] == ["mcpelevator"]


def test_tool_calls_go_through_the_api(client):
    is_error, created = _call(
        client, "create_server", {"spec": {"name": "echo", "runner": "command", "command": "echo"}}
    )
    assert not is_error and created["enabled"] is False
    is_error, detail = _call(client, "get_server", {"server_id": "nope"})
    assert is_error and "404" in json.dumps(detail)
    is_error, _ = _call(client, "delete_server", {"server_id": created["id"]})
    assert not is_error


def test_global_switch_is_the_builtin_enabled_flag(client):
    assert client.get("/api/settings", headers=LOOPBACK).json()["control_mcp"] is True
    r = client.patch("/api/settings", json={"control_mcp": False}, headers=LOOPBACK)
    assert r.status_code == 200 and r.json()["control_mcp"] is False
    assert _builtin().enabled is False
    assert _rpc(client, "tools/list")[0] == 404
    client.post(f"/api/servers/{_builtin().id}/enable", headers=LOOPBACK)
    assert client.get("/api/settings", headers=LOOPBACK).json()["control_mcp"] is True


def test_global_switch_refused_when_not_provisioned(monkeypatch):
    monkeypatch.setattr(Supervisor, "run_forever", _parked)
    with TestClient(app) as c:
        assert c.get("/api/settings", headers=LOOPBACK).json()["control_mcp"] is False
        r = c.patch("/api/settings", json={"control_mcp": True, "idle_timeout_s": 7}, headers=LOOPBACK)
        assert r.status_code == 409
        assert c.get("/api/settings", headers=LOOPBACK).json()["idle_timeout_s"] != 7


def test_enforced_gate_and_per_user_switch(client):
    admin = mint_token("control")
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    assert _rpc(client, "tools/list")[0] == 401
    # A data-plane token is not a control credential.
    assert _rpc(client, "tools/list", headers={"authorization": f"Bearer {mint_token()}"})[0] == 403

    # The built-in bridge's discovery probe lists tools; a call still needs a credential.
    internal = {INTERNAL_HEADER: INTERNAL_SECRET}
    assert _rpc(client, "tools/list", headers=internal)[0] == 200
    is_error, out = _call(client, "list_servers", headers=internal)
    assert is_error and "401" in json.dumps(out)

    auth = {"authorization": f"Bearer {admin}"}
    user = client.post("/api/users", json={"name": "Ada"}, headers={**LOOPBACK, **auth}).json()
    assert user["control_mcp"] is True
    raw = new_token()
    with Session(get_engine()) as s:
        repo.create_token(
            s,
            Token(id=new_id(), name="ada", token_hash=hash_token(raw), prefix=raw[:12],
                  scope="control", user_id=user["id"]),
        )
    ada = {"authorization": f"Bearer {raw}"}
    is_error, servers = _call(client, "list_servers", headers=ada)
    assert not is_error and servers == {"items": []}  # a member sees only their own servers

    is_error, out = _call(client, "update_user", {"user_id": user["id"], "control_mcp": False}, headers=auth)
    assert not is_error and out["control_mcp"] is False
    assert _rpc(client, "tools/list", headers=ada)[0] == 403
    assert _rpc(client, "tools/list", headers=auth)[0] == 200


def test_builtin_row_guards(client):
    b = _builtin()
    url = f"/api/servers/{b.id}"
    assert client.patch(url, json={"command": "evil"}, headers=LOOPBACK).status_code == 400
    assert client.patch(url, json={"auth_provider": "oauth"}, headers=LOOPBACK).status_code == 400
    assert client.patch(url, json={"auth_provider": "inherit"}, headers=LOOPBACK).status_code == 400
    r = client.patch(
        url,
        json={"name": "Control", "runner": BUILTIN_RUNNER, "env": {}, "disabled_tools": ["delete_user"]},
        headers=LOOPBACK,
    )
    assert r.status_code == 200, r.text
    assert _builtin().disabled_tools == ["delete_user"]
    assert client.delete(url, headers=LOOPBACK).status_code == 409
    assert client.post(f"{url}/clone", headers=LOOPBACK).status_code == 400
    assert client.post("/api/servers", json={"name": "x", "runner": BUILTIN_RUNNER, "command": "x"},
                       headers=LOOPBACK).status_code == 400
    other = client.post("/api/servers", json={"name": "y", "runner": "command", "command": "echo"},
                        headers=LOOPBACK).json()
    r = client.patch(f"/api/servers/{other['id']}", json={"runner": BUILTIN_RUNNER}, headers=LOOPBACK)
    assert r.status_code == 400

    # Wildcard groups skip it and an explicit member list can't name it.
    with Session(get_engine()) as s:
        registry.write_group(s, "all", "*")
        assert b.id not in registry.resolve(s, "all")
        with pytest.raises(ValueError):
            registry.write_group(s, "ops", [b.id])


def test_playground_forwards_the_callers_credential_to_the_builtin_only(client, monkeypatch):
    from types import SimpleNamespace

    from app.api import servers as servers_api

    seen = []

    async def fake_call(url, name, arguments, timeout, headers=None):
        seen.append(headers)
        return SimpleNamespace(is_error=False, content=[], structured_content=None)

    monkeypatch.setattr(servers_api, "_call_bridge_tool", fake_call)
    b = _builtin()
    unit = SimpleNamespace(state="running", port=1, host="127.0.0.1", tools=[{"name": "list_servers"}])
    sup = client.app.state.supervisor
    monkeypatch.setattr(sup, "unit", lambda sid: unit)
    auth = {**LOOPBACK, "authorization": "Bearer abc"}
    r = client.post(f"/api/servers/{b.id}/tools/list_servers/call", json={}, headers=auth)
    assert r.status_code == 200, r.text
    other = client.post("/api/servers", json={"name": "z", "runner": "command", "command": "echo"},
                        headers=LOOPBACK).json()
    unit.tools = [{"name": "list_servers"}]
    client.post(f"/api/servers/{other['id']}/tools/list_servers/call", json={}, headers=auth)
    assert seen == [{"authorization": "Bearer abc"}, None]


def test_logs_recheck_visibility_after_the_await(client, monkeypatch):
    from types import SimpleNamespace

    from app.api import servers as servers_api

    b = _builtin()
    logs = SimpleNamespace(snapshot=lambda: ["secret line"])
    unit = SimpleNamespace(state="running", logs=logs, config_hash=b.config_hash, pid=1, port=1,
                           host="127.0.0.1", last_error=None, startup_status=None, tools=[])
    monkeypatch.setattr(client.app.state.supervisor, "unit", lambda sid: unit)
    is_error, out = _call(client, "get_server_logs", {"server_id": b.id})
    assert not is_error and out["lines"] == ["secret line"]
    monkeypatch.setattr(servers_api, "_visible_now", lambda sid, p: False)  # transferred away
    is_error, out = _call(client, "get_server_logs", {"server_id": b.id})
    assert is_error and "secret" not in json.dumps(out)


def test_bearer_accepts_control_tokens_for_the_builtin_only(client):
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth.bearer import BearerProvider

    token = mint_token("control")
    req = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
    asyncio.run(BearerProvider().authenticate(req, _builtin()))
    other = Server(id="other", slug="other", name="other", args=[], env={})
    with pytest.raises(HTTPException):
        asyncio.run(BearerProvider().authenticate(req, other))
