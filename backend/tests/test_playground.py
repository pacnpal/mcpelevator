"""Tool-playground endpoint tests: POST /api/servers/{id}/tools/{name}/call.

The bridge hop (``_call_bridge_tool``) is monkeypatched — these tests cover the
control-plane contract (gating, lookup, result mapping), not FastMCP transport.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from conftest import LOOPBACK, create_server

from app.api import servers as servers_api
from app.main import app


class _FakeContent:
    def __init__(self, payload: dict):
        self._payload = payload

    def model_dump(self, mode: str = "json") -> dict:
        return self._payload


def _fake_result(*, is_error=False, content=None, structured=None):
    return SimpleNamespace(
        is_error=is_error,
        content=[_FakeContent(c) for c in (content or [])],
        structured_content=structured,
    )


def _running_unit(tools: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(state="running", host="127.0.0.1", port=49999, tools=tools)


def test_call_unknown_server_404():
    with TestClient(app) as client:
        r = client.post(
            "/api/servers/nope/tools/echo/call", json={"arguments": {}}, headers=LOOPBACK
        )
        assert r.status_code == 404


def test_call_not_running_409():
    with TestClient(app) as client:
        srv = create_server(client, name="pg-stopped")
        try:
            r = client.post(
                f"/api/servers/{srv['id']}/tools/echo/call",
                json={"arguments": {}},
                headers=LOOPBACK,
            )
            assert r.status_code == 409
            assert "not running" in r.text
        finally:
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_call_unknown_tool_404():
    with TestClient(app) as client:
        srv = create_server(client, name="pg-tools")
        try:
            client.app.state.supervisor.units[srv["id"]] = _running_unit(
                [{"name": "echo", "description": "", "input_schema": {}}]
            )
            r = client.post(
                f"/api/servers/{srv['id']}/tools/other/call",
                json={"arguments": {}},
                headers=LOOPBACK,
            )
            assert r.status_code == 404
            assert "other" in r.text
        finally:
            client.app.state.supervisor.units.pop(srv["id"], None)
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_call_success_maps_result(monkeypatch):
    seen: dict = {}

    async def fake_call(url, name, arguments, timeout):
        seen.update(url=url, name=name, arguments=arguments, timeout=timeout)
        return _fake_result(
            content=[{"type": "text", "text": "4"}], structured={"result": 4}
        )

    monkeypatch.setattr(servers_api, "_call_bridge_tool", fake_call)
    with TestClient(app) as client:
        srv = create_server(client, name="pg-ok")
        try:
            client.app.state.supervisor.units[srv["id"]] = _running_unit(
                [{"name": "add", "description": "", "input_schema": {}}]
            )
            r = client.post(
                f"/api/servers/{srv['id']}/tools/add/call",
                json={"arguments": {"a": 2, "b": 2}},
                headers=LOOPBACK,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["is_error"] is False
            assert body["content"] == [{"type": "text", "text": "4"}]
            assert body["structured_content"] == {"result": 4}
            assert body["duration_ms"] >= 0
            # the bridge hop got the loopback URL and the caller's arguments
            assert seen["url"] == "http://127.0.0.1:49999/mcp"
            assert seen["name"] == "add"
            assert seen["arguments"] == {"a": 2, "b": 2}
        finally:
            client.app.state.supervisor.units.pop(srv["id"], None)
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_call_tool_error_is_200_with_is_error(monkeypatch):
    """MCP semantics: a tool's own failure is data (is_error), not an HTTP error."""

    async def fake_call(url, name, arguments, timeout):
        return _fake_result(is_error=True, content=[{"type": "text", "text": "boom"}])

    monkeypatch.setattr(servers_api, "_call_bridge_tool", fake_call)
    with TestClient(app) as client:
        srv = create_server(client, name="pg-err")
        try:
            client.app.state.supervisor.units[srv["id"]] = _running_unit(
                [{"name": "boomer", "description": "", "input_schema": {}}]
            )
            r = client.post(
                f"/api/servers/{srv['id']}/tools/boomer/call",
                json={"arguments": {}},
                headers=LOOPBACK,
            )
            assert r.status_code == 200
            assert r.json()["is_error"] is True
        finally:
            client.app.state.supervisor.units.pop(srv["id"], None)
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)


def test_call_transport_failure_502(monkeypatch):
    async def fake_call(url, name, arguments, timeout):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(servers_api, "_call_bridge_tool", fake_call)
    with TestClient(app) as client:
        srv = create_server(client, name="pg-down")
        try:
            client.app.state.supervisor.units[srv["id"]] = _running_unit(
                [{"name": "echo", "description": "", "input_schema": {}}]
            )
            r = client.post(
                f"/api/servers/{srv['id']}/tools/echo/call",
                json={"arguments": {}},
                headers=LOOPBACK,
            )
            assert r.status_code == 502
            assert "connection refused" in r.text
        finally:
            client.app.state.supervisor.units.pop(srv["id"], None)
            client.delete(f"/api/servers/{srv['id']}", headers=LOOPBACK)
