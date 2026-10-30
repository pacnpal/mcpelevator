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
from app.db.models import BUILTIN_RUNNER, RUNNERS, Server, Token, User
from app.groups import registry
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.runners.builtin import INSTRUCTIONS, INTERNAL_HEADER, INTERNAL_SECRET
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


def _description(client, tool: str) -> str:
    _, msg = _rpc(client, "tools/list")
    return next(t for t in msg["result"]["tools"] if t["name"] == tool)["description"]


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


def test_mixed_settings_patch_with_toggle_is_atomic(client, monkeypatch):
    from app.auth import principal as principal_mod

    monkeypatch.setattr(principal_mod, "admin_now", lambda s, p: False)
    admin = mint_token("control")
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    auth = {**LOOPBACK, "authorization": f"Bearer {admin}"}
    r = client.patch("/api/settings", json={"control_mcp": False, "idle_timeout_s": 9}, headers=auth)
    assert r.status_code == 403
    assert _builtin().enabled is True
    assert client.get("/api/settings", headers=auth).json()["idle_timeout_s"] != 9


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
    # An anonymous client reaching the bridge through a group is not that probe.
    assert _rpc(client, "tools/list", headers={**internal, "x-mcpe-group": "g"})[0] == 401

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
    assert client.patch(url, json={"rest_openapi": True}, headers=LOOPBACK).status_code == 400
    ada = client.post("/api/users", json={"name": "Ada"}, headers=LOOPBACK).json()
    assert client.patch(url, json={"owner_id": ada["id"]}, headers=LOOPBACK).status_code == 400
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

    # Wildcard groups skip it; it joins a group only when named.
    with Session(get_engine()) as s:
        registry.write_group(s, "all", "*")
        assert b.id not in registry.resolve(s, "all")
        assert not registry.includes_builtin(s, "all")
        registry.write_group(s, "ops", [b.id])
        assert registry.resolve(s, "ops") == [b.id]
        assert registry.includes_builtin(s, "ops")


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
    assert seen == [{"authorization": "Bearer abc", "x-mcpe-original-host": "127.0.0.1"}, None]
    # An OAuth-only Cloudflare client has no Authorization; its assertion goes instead.
    seen.clear()
    cf = {**LOOPBACK, "cf-access-jwt-assertion": "a.b.c"}
    client.post(f"/api/servers/{b.id}/tools/list_servers/call", json={}, headers=cf)
    assert seen == [{"cf-access-jwt-assertion": "a.b.c", "x-mcpe-original-host": "127.0.0.1"}]


def test_logs_recheck_visibility_after_the_await(client, monkeypatch):
    from types import SimpleNamespace

    from app.api import servers as servers_api

    b = _builtin()
    logs = SimpleNamespace(snapshot=lambda: ["secret line"])
    unit = SimpleNamespace(state="running", logs=logs, config_hash=b.config_hash, pid=1, port=1,
                           host="127.0.0.1", last_error=None, startup_status=None, tools=[])
    monkeypatch.setattr(client.app.state.supervisor, "unit", lambda sid: unit)
    is_error, out = _call(client, "get_server_logs", {"server_id": b.id})
    assert not is_error and out["lines"] == ["secret line"] and out["running"] is True
    unit.state = "failed"
    is_error, out = _call(client, "get_server_logs", {"server_id": b.id})
    assert out["running"] is False and out["state"] == "failed"
    unit.state = "running"
    monkeypatch.setattr(servers_api, "_visible_now", lambda sid, p: False)  # transferred away
    is_error, out = _call(client, "get_server_logs", {"server_id": b.id})
    assert is_error and "secret" not in json.dumps(out)


def test_builtin_refuses_to_restart_itself_through_its_bridge(client):
    b = _builtin()
    internal = {INTERNAL_HEADER: INTERNAL_SECRET}
    is_error, out = _call(client, "restart_server", {"server_id": b.id}, headers=internal)
    assert is_error and "itself" in json.dumps(out)


def test_internal_url_follows_the_bind_family(monkeypatch):
    from app.config import get_settings
    from app.runners.builtin import internal_url

    for host, want in (("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("10.0.0.5", "10.0.0.5")):
        monkeypatch.setattr(get_settings(), "host", host)
        assert internal_url().startswith(f"http://{want}:")


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


def test_whoami_and_wait_for_server(client):
    is_error, me = _call(client, "whoami")
    assert not is_error and me["user"]["role"] == "admin"
    created = client.post("/api/servers", json={"name": "w", "runner": "command", "command": "echo"},
                          headers=LOOPBACK).json()
    is_error, out = _call(client, "wait_for_server", {"server_id": created["id"], "timeout_s": 2})
    assert not is_error and out["state"] == "stopped" and out["timed_out"] is False


def test_install_from_catalog(client, monkeypatch):
    from app.catalog import base, registry as catalog_registry

    for src in catalog_registry.SOURCES.values():
        if getattr(src, "_cache", None) is not None:
            src._cache._store.clear()
    detail = {
        "server": {
            "name": "io.example/memory", "title": "Memory", "version": "1.0.0",
            "packages": [{"registryType": "npm", "identifier": "@me/memory", "version": "1.0.0",
                          "transport": {"type": "stdio"}}],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
    }

    async def fake_get_json(http, url, params):
        return detail

    monkeypatch.setattr(base, "get_json", fake_get_json)
    is_error, out = _call(
        client, "install_from_catalog", {"id": "io.example/memory", "enabled": False}
    )
    assert not is_error, out
    srv = out["server"]
    assert (srv["runner"], srv["name"], srv["enabled"]) == ("npx", "Memory", False)
    with Session(get_engine()) as s:
        assert repo.get_server(s, srv["id"]).source == "catalog:io.example/memory@1.0.0"


def test_create_server_names_every_runner(client):
    # The tool description is the only documentation an agent sees: a new runner
    # that skips the control MCP fails here.
    description = _description(client, "create_server")
    missing = [r for r in RUNNERS if r not in description]
    assert not missing, f"create_server doesn't describe runner(s) {missing}"


def test_openapi_server_through_the_control_mcp(client):
    assert "openapi" in _description(client, "update_server")
    # The instructions carry the flow, down to the one log line that explains a failure.
    flow = next(line for line in INSTRUCTIONS.split("\n- ") if "OpenAPI document" in line)
    assert '"openapi"' in flow and "wait_for_server" in flow and '"openapi: ..."' in flow
    spec = {
        "name": "petstore",
        "runner": "openapi",
        "command": "https://petstore.example/openapi.json",
        "args": ["https://api.petstore.example/v3"],
        "env": {"Authorization": "Bearer k"},
    }
    is_error, created = _call(client, "create_server", {"spec": spec})
    assert not is_error, created
    assert created["enabled"] is False
    is_error, detail = _call(client, "get_server", {"server_id": created["id"]})
    assert not is_error
    assert (detail["runner"], detail["command"], detail["args"], detail["env"]) == (
        "openapi",
        spec["command"],
        spec["args"],
        spec["env"],
    )
    # A document that isn't a URL is the API's 400, surfaced as a tool error.
    is_error, refused = _call(
        client,
        "create_server",
        {"spec": {"name": "bad", "runner": "openapi", "command": "/etc/openapi.json"}},
    )
    assert is_error and "http(s)://" in json.dumps(refused)


def test_resources_and_prompts(client):
    status, msg = _rpc(client, "resources/list")
    assert status == 200
    assert {"mcpelevator://servers", "mcpelevator://settings", "mcpelevator://groups"} <= {
        r["uri"] for r in msg["result"]["resources"]
    }
    status, msg = _rpc(client, "resources/read", {"uri": "mcpelevator://servers"})
    assert "mcpelevator" in msg["result"]["contents"][0]["text"]
    status, msg = _rpc(client, "resources/templates/list")
    assert "mcpelevator://servers/{server_id}/logs" in {
        t["uriTemplate"] for t in msg["result"]["resourceTemplates"]
    }
    status, msg = _rpc(client, "prompts/list")
    assert {"install_server", "diagnose_server"} <= {p["name"] for p in msg["result"]["prompts"]}


def test_path_arguments_cannot_escape_their_route(client):
    token = client.post("/api/tokens", json={"name": "keep"}, headers=LOOPBACK).json()
    for bad in (f"../tokens/{token['id']}", ".."):
        is_error, _ = _call(client, "delete_server", {"server_id": bad})
        assert is_error
    assert any(t["id"] == token["id"] for t in client.get("/api/tokens", headers=LOOPBACK).json())


def test_guidance_uses_the_operators_tool_names(client):
    from app.runners.builtin import INSTRUCTIONS, localize

    b = _builtin()
    b.tool_overrides = {"wait_for_server": {"name": "await_server"}}
    b.disabled_tools = ["delete_user"]
    text = localize(INSTRUCTIONS, b)
    assert "await_server" in text and "wait_for_server" not in text
    assert "Hidden on this server, so not callable: delete_user." in text


def test_install_from_catalog_drops_warnings_the_caller_filled(client, monkeypatch):
    from app.catalog import base, registry as catalog_registry

    for src in catalog_registry.SOURCES.values():
        if getattr(src, "_cache", None) is not None:
            src._cache._store.clear()
    detail = {
        "server": {
            "name": "io.example/keyed", "title": "Keyed", "version": "1.0.0",
            "packages": [{"registryType": "npm", "identifier": "@me/keyed", "version": "1.0.0",
                          "transport": {"type": "stdio"},
                          "environmentVariables": [{"name": "API_KEY", "isRequired": True,
                                                    "isSecret": True}]}],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
    }

    async def fake_get_json(http, url, params):
        return detail

    monkeypatch.setattr(base, "get_json", fake_get_json)
    is_error, out = _call(client, "install_from_catalog", {"id": "io.example/keyed"})
    assert not is_error and out["warnings"] and out["server"]["enabled"] is False
    # A key that only happens to be a word in the warning ("set its value") fills nothing.
    is_error, out = _call(
        client, "install_from_catalog", {"id": "io.example/keyed", "env": {"value": "x"}}
    )
    assert not is_error and out["warnings"] and out["server"]["enabled"] is False
    is_error, out = _call(
        client, "install_from_catalog", {"id": "io.example/keyed", "env": {"API_KEY": "k"}}
    )
    assert not is_error and out["warnings"] == [] and out["server"]["enabled"] is True


def _stub_repository(monkeypatch, package_json: str | None):
    from app.catalog import repository

    async def fake_fetch_text(http, url):
        return package_json if url.endswith("/package.json") else None

    async def fake_file_exists(http, url):
        return url.endswith("/package-lock.json")

    monkeypatch.setattr(repository, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(repository, "file_exists", fake_file_exists)


_PKG = '{"name": "srv", "bin": "dist/index.js", "scripts": {"build": "tsc"}}'


def test_get_repository_draft(client, monkeypatch):
    _stub_repository(monkeypatch, _PKG)
    is_error, out = _call(client, "get_repository_draft", {"url": "https://github.com/o/srv"})
    assert not is_error, out
    draft = out["drafts"][0]
    assert (draft["command"], draft["args"]) == ("node", ["dist/index.js"])
    assert draft["post_clone_script"] == "npm ci\nnpm run build"


def test_install_from_repository_detects_and_takes_overrides(client, monkeypatch):
    _stub_repository(monkeypatch, _PKG)
    is_error, out = _call(
        client,
        "install_from_repository",
        {"url": "https://github.com/o/srv/tree/v2", "post_clone_script": "npm ci",
         "enabled": False},
    )
    assert not is_error, out
    srv = out["server"]
    detail = client.get(f"/api/servers/{srv['id']}", headers=LOOPBACK).json()
    assert (detail["runner"], detail["command"], detail["args"]) == (
        "command", "node", ["dist/index.js"]
    )
    assert (detail["repo_url"], detail["repo_ref"]) == ("https://github.com/o/srv", "v2")
    assert detail["post_clone_script"] == "npm ci"  # the override, not the detected steps
    assert detail["source"] == "github:o/srv#v2"
    assert srv["enabled"] is False

    # Detection failing with no command given is an error that says what to pass.
    _stub_repository(monkeypatch, None)
    is_error, out = _call(client, "install_from_repository", {"url": "o/empty"})
    assert is_error and "pass command" in str(out)

    # Given a command, an undetectable repo installs, and says what detection left blank.
    is_error, out = _call(
        client, "install_from_repository",
        {"url": "o/empty", "command": "node", "args": ["server.js"], "enabled": False},
    )
    assert not is_error, out
    assert any("No package.json or pyproject.toml" in n for n in out["notes"])


def test_annotations_only_call_additive_tools_non_destructive(client):
    status, msg = _rpc(client, "tools/list")
    hints = {t["name"]: (t.get("annotations") or {}).get("destructiveHint") for t in msg["result"]["tools"]}
    for name in ("update_settings", "update_server", "update_user", "put_group", "call_server_tool"):
        assert hints[name] is not False, name
    for name in ("create_server", "create_token", "install_from_catalog", "install_from_repository"):
        assert hints[name] is False, name


def test_break_glass_token_opens_the_builtin(client, monkeypatch):
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth.bearer import BearerProvider
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "break-glass")
    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer break-glass")]})
    asyncio.run(BearerProvider().authenticate(req, _builtin()))
    other = Server(id="other", slug="other", name="other", args=[], env={})
    with pytest.raises(HTTPException):
        asyncio.run(BearerProvider().authenticate(req, other))


def test_the_s_proxy_sets_the_builtin_markers_itself(client):
    """x-mcpe-original-host and x-mcpe-group are the proxy's and the group route's to
    set. A client's copy on /s must not reach the built-in bridge."""
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def echo(request):
        return JSONResponse({k: v for k, v in request.headers.items() if k.startswith("x-mcpe-")})

    upstream = Starlette(routes=[Route("/{path:path}", echo, methods=["POST"])])
    client.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream))
    client.app.state.supervisor.endpoint = lambda slug: ("bridge", 9000)
    forged = {"x-mcpe-group": "g", "x-mcpe-original-host": "evil.example",
              "authorization": f"Bearer {mint_token('control')}"}
    r = client.post(f"/s/{_builtin().slug}/mcp", json={}, headers={**LOOPBACK, **forged})
    assert r.json() == {"x-mcpe-original-host": "127.0.0.1"}


def test_urls_name_the_host_the_caller_used(client):
    carried = {"x-mcpe-original-host": "localhost:9999"}
    _, out = _call(client, "list_servers", headers=carried)  # no internal secret: ignored
    assert out["items"][0]["urls"]["mcp"].startswith("http://127.0.0.1/")
    _, out = _call(client, "list_servers", headers={**carried, INTERNAL_HEADER: INTERNAL_SECRET})
    assert out["items"][0]["urls"]["mcp"].startswith("http://localhost:9999/")


def test_per_user_switch_applies_with_control_auth_off(client):
    user = client.post("/api/users", json={"name": "Bo", "control_mcp": False}, headers=LOOPBACK).json()
    raw = new_token()
    with Session(get_engine()) as s:
        repo.create_token(
            s,
            Token(id=new_id(), name="bo", token_hash=hash_token(raw), prefix=raw[:12],
                  scope="all", user_id=user["id"]),
        )
    assert _rpc(client, "tools/list", headers={"authorization": f"Bearer {raw}"})[0] == 403
    assert _rpc(client, "tools/list")[0] == 200  # the local operator, no token


def test_builtin_refuses_to_reconfigure_itself_through_its_bridge(client):
    b = _builtin()
    internal = {INTERNAL_HEADER: INTERNAL_SECRET}
    is_error, out = _call(client, "update_server",
                          {"server_id": b.id, "changes": {"disabled_tools": ["whoami"]}}, headers=internal)
    assert is_error and "itself" in json.dumps(out)
    is_error, out = _call(client, "disable_server", {"server_id": b.id}, headers=internal)
    assert is_error and "itself" in json.dumps(out)
    is_error, out = _call(client, "update_settings", {"changes": {"control_mcp": False}},
                          headers=internal)
    assert is_error and "itself" in json.dumps(out) and _builtin().enabled
    is_error, out = _call(client, "update_server",
                          {"server_id": b.id, "changes": {"name": "Ctl"}}, headers=internal)
    assert not is_error and out["name"] == "Ctl"


def test_non_ascii_internal_header_is_just_a_mismatch(client):
    r = client.post(
        "/api/mcp",
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={**MCP_HEADERS, "content-type": "application/json",
                 INTERNAL_HEADER: b"\xe9"},
    )
    assert r.status_code == 200  # auth off: the local operator, not a 500


def test_prompts_use_bridge_names_only_through_the_bridge(client):
    with Session(get_engine()) as s:
        b = service.get_builtin(s)
        b.tool_overrides = {"wait_for_server": {"name": "await_server"}}
        s.add(b)
        s.commit()
        slug = b.slug
    args = {"name": "install_server", "arguments": {"what": "x"}}

    def text(headers=None):
        _, msg = _rpc(client, "prompts/get", args, headers)
        return msg["result"]["messages"][0]["content"]["text"]

    assert "wait_for_server" in text()  # direct /api/mcp: the tools aren't renamed here
    assert "await_server" in text({INTERNAL_HEADER: INTERNAL_SECRET})
    # Through a group bundle every tool carries the slug, renamed ones included.
    grouped = text({INTERNAL_HEADER: INTERNAL_SECRET, "x-mcpe-group": "g"})
    assert f"{slug}_await_server" in grouped and f"{slug}_search_catalog" in grouped
    assert " search_catalog" not in grouped


def _member_control_token(client, admin_headers: dict, name: str = "Ada") -> tuple[dict, str]:
    user = client.post("/api/users", json={"name": name}, headers={**LOOPBACK, **admin_headers}).json()
    raw = new_token()
    with Session(get_engine()) as s:
        repo.create_token(
            s,
            Token(id=new_id(), name=name, token_hash=hash_token(raw), prefix=raw[:12],
                  scope="control", user_id=user["id"]),
        )
    return user, raw


def test_groups_naming_the_builtin_open_to_admin_control_credentials(client):
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth.bearer import BearerProvider
    from app.groups.hub import group_server

    admin = mint_token("control")
    _, member = _member_control_token(client, {"authorization": f"Bearer {admin}"})
    with Session(get_engine()) as s:
        registry.write_group(s, "ops", [_builtin().id])
        registry.write_group(s, "plain", [])

    def auth(token: str, group: str) -> None:
        req = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
        asyncio.run(BearerProvider().authenticate(req, group_server(group)))

    auth(admin, "ops")
    with pytest.raises(HTTPException):
        auth(member, "ops")  # a member's login must not reach the other members' tools
    with pytest.raises(HTTPException):
        auth(admin, "plain")  # a control token opens only groups bundling the control plane
    auth(mint_token("group:plain"), "plain")


def test_hub_forwards_headers_to_the_builtin_member_only():
    from app.groups.hub import GroupHub, _default_transport

    captured = []

    def factory(url: str):
        captured.append(_default_transport(url))
        return captured[-1]

    hub = GroupHub(transport_factory=factory)
    hub._make_proxy("mcpelevator", "http://127.0.0.1:1/mcp", forward=True)
    hub._make_proxy("other", "http://127.0.0.1:2/mcp")
    assert [t.forward_incoming_headers for t in captured] == [True, False]


@pytest.fixture
def cf(monkeypatch):
    """Cloudflare Access configured for team.example, signing with a local key."""
    import time
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.auth import cf_access
    from app.config import get_settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(get_settings(), "cf_access_team_domain", "team.example")
    monkeypatch.setattr(get_settings(), "cf_access_aud", "aud-1")
    fetches = []

    def get_signing_keys(refresh: bool = False):
        fetches.append(refresh)
        return [SimpleNamespace(key_id="k1", key=key.public_key())]

    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(
        get_signing_keys=get_signing_keys, jwk_set_cache=None))
    monkeypatch.setattr(cf_access, "_last_refetch", {})
    monkeypatch.setattr(cf_access, "_last_failure", {})
    # As the startup load leaves it: requests on the event loop answer from these.
    monkeypatch.setattr(cf_access, "_good", {"https://team.example": ({"k1": key.public_key()}, time.monotonic())})

    def assertion(email: str, aud: str = "aud-1", kid: str = "k1", exp: int = 60,
                  iss: str = "https://team.example") -> dict:
        now = int(time.time())
        claims = {"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp}
        return {cf_access.HEADER: jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})}

    assertion.fetches = fetches

    return assertion


def test_cloudflare_identity_runs_calls_as_the_matching_user(client, cf):
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth.bearer import BearerProvider
    from app.control_mcp import _authorize

    admin = {"authorization": f"Bearer {mint_token('control')}"}
    client.post("/api/users", json={"name": "ada@example.com"}, headers={**LOOPBACK, **admin})
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    internal = {INTERNAL_HEADER: INTERNAL_SECRET}

    # Through the built-in bridge, the assertion names the caller, email case aside.
    is_error, me = _call(client, "whoami", headers={**internal, **cf("Ada@Example.com")})
    assert not is_error and me["user"]["name"] == "ada@example.com" and me["user"]["role"] == "member"
    # Straight to /api/mcp, without the bridge, it isn't trusted.
    status, body = _rpc(client, "tools/list", headers=cf("ada@example.com"))
    assert status == 403 and "only through" in body["detail"]

    def edge(headers: dict) -> None:
        req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
        with Session(get_engine()) as s:
            _authorize(req, s, edge=True)

    edge(cf("ada@example.com"))
    asyncio.run(BearerProvider().authenticate(
        Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]}),
        _builtin(),
    ))
    for headers, reason in (
        (cf("bob@example.com"), "no single user has that name"),
        (cf("ada@example.com", aud="other"), "different Access application"),
        (cf("ada@example.com", iss="https://other.example"), "different team domain"),
        (cf("ada@example.com", exp=-120), "expired"),
        (cf("ada@example.com", kid="nope"), "didn't verify"),
        ({"authorization": f"Bearer {mint_token()}"}, "isn't one"),
    ):
        with pytest.raises(HTTPException) as exc:
            edge(headers)
        assert exc.value.status_code == 403 and reason in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        edge({})
    assert exc.value.status_code == 401


def test_cloudflare_identity_refuses_an_ambiguous_name(client, cf):
    from starlette.requests import Request

    from app.auth import cf_access

    admin = {**LOOPBACK, "authorization": f"Bearer {mint_token('control')}"}
    client.post("/api/users", json={"name": "ada@example.com"}, headers=admin)
    req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]})
    with Session(get_engine()) as s:
        assert cf_access.user(s, req) is not None
    client.post("/api/users", json={"name": "ADA@example.com ", "role": "admin"}, headers=admin)
    with Session(get_engine()) as s:
        assert cf_access.user(s, req) is None


def test_bridge_forwarding_carries_the_cloudflare_assertion(monkeypatch):
    """The built-in bridge and the group hub rely on FastMCP's forward_incoming_headers
    passing every caller header on, not just Authorization. Pin that."""
    from starlette.requests import Request

    import fastmcp.server.dependencies as deps

    req = Request({"type": "http", "headers": [(b"authorization", b"Bearer x"),
                                               (b"cf-access-jwt-assertion", b"a.b.c")]})
    monkeypatch.setattr(deps, "get_http_request", lambda: req)
    assert {"authorization", "cf-access-jwt-assertion"} <= set(deps.get_http_headers(include={"authorization"}))


def test_unknown_signing_key_refetches_at_most_once_per_window(cf):
    from starlette.requests import Request

    from app.auth import cf_access

    def request(headers: dict) -> Request:
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})

    for _ in range(5):
        assert cf_access.email(request(cf("ada@example.com", kid="junk"))) is None
    assert cf.fetches.count(True) == 1  # junk kids can't force a fetch per request
    assert cf_access.email(request(cf("ada@example.com"))) == "ada@example.com"


def test_unreachable_signing_keys_back_off_instead_of_blocking_each_request(monkeypatch, cf):
    from types import SimpleNamespace

    import jwt
    from starlette.requests import Request

    from app.auth import cf_access

    calls = []

    def down(refresh: bool = False):
        calls.append(refresh)
        raise jwt.PyJWKClientConnectionError("timed out")

    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(
        get_signing_keys=down, jwk_set_cache=None))
    req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]})
    for _ in range(5):
        assert cf_access.email(req) is None
    assert len(calls) == 1  # one blocking attempt, then the backoff answers

    # A 200 that isn't a key set (bad JSON) is refused and backed off the same way.
    import json

    calls.clear()
    cf_access._last_failure.clear()

    def garbled(refresh: bool = False):
        calls.append(refresh)
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(
        get_signing_keys=garbled, jwk_set_cache=None))
    for _ in range(5):
        assert cf_access.verify(req) == (
            None, "Cloudflare Access's signing keys aren't available yet; try again shortly")
    assert len(calls) == 1


def test_a_cached_key_set_with_no_keys_is_refetched(monkeypatch):
    """{"keys": []} gets cached like any answer; serving it would fail every sign-in
    until it expired, so a cache hit that fails refetches (under the backoff)."""
    import jwt
    from types import SimpleNamespace

    from app.auth import cf_access

    calls = []

    def keys(refresh: bool = False):
        calls.append(refresh)
        if not refresh:  # what PyJWT raises for {"keys": []}, a sibling of PyJWKClientError
            raise jwt.PyJWKSetError("The JWK Set did not contain any keys")
        return [SimpleNamespace(key_id="k1", key="pem")]

    cache = SimpleNamespace(get=lambda: object())
    monkeypatch.setattr(cf_access, "_last_failure", {})
    monkeypatch.setattr(cf_access, "_last_refetch", {})
    monkeypatch.setattr(cf_access, "_good", {})
    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(
        get_signing_keys=keys, jwk_set_cache=cache))
    assert cf_access._fetch("https://team.example", refresh=False) == {"k1": "pem"}
    assert calls == [False, True]


def test_an_assertion_with_a_non_string_kid_is_refused(cf):
    import base64
    import json

    from starlette.requests import Request

    from app.auth import cf_access

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    forged = f"{part({'alg': 'RS256', 'kid': []})}.{part({'email': 'ada@example.com'})}.c2ln"
    req = Request({"type": "http", "headers": [(b"cf-access-jwt-assertion", forged.encode())]})
    assert cf_access.verify(req) == (None, "the Cloudflare Access assertion didn't verify")


def test_sql_logging_never_shows_bound_values():
    from app.db import get_engine

    assert get_engine().hide_parameters is True


def test_backoff_never_blocks_fresh_cached_keys(monkeypatch, cf):
    import time
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.auth import cf_access

    real = cf_access._keys("https://team.example")
    cache = SimpleNamespace(get=lambda: object())  # a fresh cached key set
    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(
        get_signing_keys=real.get_signing_keys, jwk_set_cache=cache))
    cf_access._last_failure["https://team.example"] = time.monotonic()  # a refetch just failed
    req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]})
    assert cf_access.email(req) == "ada@example.com"


def test_cloudflare_admin_passes_the_settings_lockout_guard(client, cf):
    from starlette.requests import Request

    from app.auth.control_plane import admin_credential_presented

    admin = {**LOOPBACK, "authorization": f"Bearer {mint_token('control')}"}
    client.post("/api/users", json={"name": "root@example.com", "role": "admin"}, headers=admin)
    client.post("/api/users", json={"name": "ada@example.com"}, headers=admin)

    def presented(email: str, internal: bool) -> bool:
        headers = {**cf(email), **({INTERNAL_HEADER: INTERNAL_SECRET} if internal else {})}
        req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
        with Session(get_engine()) as s:
            return admin_credential_presented(req, s)

    assert presented("root@example.com", internal=True)
    assert not presented("ada@example.com", internal=True)  # a member never counts
    assert not presented("root@example.com", internal=False)  # nor an untrusted assertion


def test_restart_group_refuses_its_own_bridge(client):
    with Session(get_engine()) as s:
        registry.write_group(s, "ops", [_builtin().id])
        registry.write_group(s, "plain", [])
    internal = {INTERNAL_HEADER: INTERNAL_SECRET}
    is_error, out = _call(client, "restart_group", {"name": "ops"}, headers=internal)
    assert is_error and "can't restart itself" in json.dumps(out)
    is_error, _ = _call(client, "restart_group", {"name": "plain"}, headers=internal)
    assert not is_error


def test_failed_refetch_doesnt_hold_the_refetch_window(monkeypatch, cf):
    from types import SimpleNamespace

    import jwt
    from starlette.requests import Request

    from app.auth import cf_access

    real = cf_access._keys("https://team.example")
    state = {"down": True}

    def get_signing_keys(refresh: bool = False):
        if refresh and state["down"]:
            raise jwt.PyJWKClientConnectionError("timed out")
        keys = real.get_signing_keys()
        return [*keys, SimpleNamespace(key_id="rotated", key=keys[0].key)] if refresh else keys

    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(
        get_signing_keys=get_signing_keys, jwk_set_cache=None))
    req = Request({"type": "http", "headers": [
        (k.encode(), v.encode()) for k, v in cf("ada@example.com", kid="rotated").items()]})
    assert cf_access.email(req) is None  # the refetch fails during the outage
    state["down"] = False
    cf_access._last_failure.clear()  # the 30s failure backoff has passed
    assert cf_access.email(req) == "ada@example.com"  # no five-minute wait


def test_debug_bridges_keep_uvicorn_off_its_own_handlers(monkeypatch):
    from app import debug
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "debug", True)
    assert debug.uvicorn_logging() == {"log_config": None}
    monkeypatch.setattr(get_settings(), "debug", False)
    assert debug.uvicorn_logging() == {}


def test_failed_cloudflare_sign_in_gets_its_reason_from_the_bearer_check(client, cf):
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth.bearer import BearerProvider
    from app.groups.hub import group_server

    admin = {**LOOPBACK, "authorization": f"Bearer {mint_token('control')}"}
    client.post("/api/users", json={"name": "ada@example.com"}, headers=admin)
    with Session(get_engine()) as s:
        registry.write_group(s, "ops", [_builtin().id])

    def auth(headers: dict, server) -> HTTPException:
        req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(BearerProvider().authenticate(req, server))
        return exc.value

    edge_token = {"authorization": "Bearer cf-edge-token"}  # what Cloudflare forwards
    err = auth({**edge_token, **cf("bob@example.com")}, _builtin())
    assert err.status_code == 403 and "no single user" in err.detail
    err = auth({**edge_token, **cf("ada@example.com")}, group_server("ops"))
    assert err.status_code == 403 and "only an admin" in err.detail


def test_switch_applies_to_cloudflare_users_with_auth_off(client, cf, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.control_mcp import _authorize

    user = client.post("/api/users", json={"name": "ada@example.com"}, headers=LOOPBACK).json()
    client.patch(f"/api/users/{user['id']}", json={"control_mcp": False}, headers=LOOPBACK)
    req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]})
    with Session(get_engine()) as s, pytest.raises(HTTPException) as exc:
        _authorize(req, s, edge=True)
    assert exc.value.status_code == 403 and "disabled for this user" in exc.value.detail

    # An admin token sent alongside outranks the Cloudflare identity, as with enforcement on.
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_token", "break-glass")
    both = [*req.scope["headers"], (b"authorization", b"Bearer break-glass")]
    with Session(get_engine()) as s:
        _authorize(Request({"type": "http", "headers": both}), s, edge=True)


def test_key_refresh_on_the_event_loop_never_waits_on_the_fetch(monkeypatch, cf):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.auth import cf_access

    req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]})
    assert cf_access.email(req) == "ada@example.com"  # off the loop: fetched in place
    real, gate, calls = cf_access._keys("https://team.example"), threading.Event(), []

    def slow(refresh: bool = False):
        calls.append(refresh)
        gate.wait(5)
        return real.get_signing_keys(refresh)

    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(get_signing_keys=slow, jwk_set_cache=None))

    async def on_loop():
        return [cf_access.email(req) for _ in range(3)]

    started = time.monotonic()
    assert asyncio.run(on_loop()) == ["ada@example.com"] * 3  # the last keys answer
    assert time.monotonic() - started < 1  # nobody waited on the stalled fetch
    gate.set()
    for _ in range(200):
        if not cf_access._refreshing:
            break
        time.sleep(0.01)
    assert calls == [False]  # one refresh in a thread, not one per request


def test_a_cold_start_on_the_event_loop_refuses_instead_of_waiting(monkeypatch, cf):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.auth import cf_access

    real, gate = cf_access._keys("https://team.example"), threading.Event()

    def slow(refresh: bool = False):
        gate.wait(5)
        return real.get_signing_keys(refresh)

    monkeypatch.setattr(cf_access, "_good", {})  # the startup load hasn't landed
    monkeypatch.setattr(cf_access, "_keys", lambda team: SimpleNamespace(get_signing_keys=slow, jwk_set_cache=None))
    req = Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in cf("ada@example.com").items()]})

    async def on_loop():
        return cf_access.verify(req)

    started = time.monotonic()
    assert asyncio.run(on_loop())[1] == "Cloudflare Access's signing keys aren't available yet; try again shortly"
    assert time.monotonic() - started < 1
    gate.set()
    for _ in range(200):
        if not cf_access._refreshing:
            break
        time.sleep(0.01)
    assert asyncio.run(on_loop()) == ("ada@example.com", None)  # loaded in the background


def test_bridge_calls_pass_the_host_check_behind_a_forwarding_proxy():
    """The bridge forwards the caller's X-Forwarded-For, so uvicorn reports the
    caller's public address as the peer of its loopback call to /api/mcp. The
    internal secret, not that address, decides whether Host: 127.0.0.1 passes."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth.middleware import enforce_host

    def request(internal: bool) -> Request:
        headers = [(b"host", b"127.0.0.1:8080")]
        if internal:
            headers.append((INTERNAL_HEADER.encode(), INTERNAL_SECRET.encode()))
        return Request({"type": "http", "headers": headers, "client": ("160.79.106.169", 0)})

    with Session(get_engine()) as s:
        enforce_host(request(internal=True), s)  # the bridge's own call passes
        with pytest.raises(HTTPException) as exc:
            enforce_host(request(internal=False), s)  # a real remote peer still doesn't
    assert exc.value.status_code == 403
