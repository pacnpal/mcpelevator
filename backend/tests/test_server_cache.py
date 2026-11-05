"""Per-server cache directories (app.runners.cache): the env a local server's setup
script and child see, the clear action (``POST /api/servers/{id}/cache/clear`` and the
control MCP's ``clear_server_cache``), and cleanup on delete."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from conftest import LOOPBACK, mint_token

from app.bridge.host import _child_env
from app.config import get_settings
from app.db import get_engine, repo
from app.db.models import Server, Token, User
from app.main import app
from app.registry import service
from app.registry import settings as runtime_settings
from app.runners import cache as server_cache
from app.supervisor.supervisor import Supervisor
from app.supervisor.unit import ServerUnit

FIXTURE = Path(__file__).with_name("stdio_server_fixture.py")


async def _parked(self):
    await asyncio.Event().wait()


def _settings(tmp_path: Path, **overrides):
    values = {
        "bridge_host": "127.0.0.1",
        "backend_root": Path(__file__).parents[1],
        "data_dir": tmp_path,
        "start_timeout_s": 30.0,
        "restart_budget": 1,
        "restart_stable_s": 0.1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fill(directory: Path) -> int:
    """A cache with nested files; returns the bytes written."""
    (directory / "ms-playwright" / "chromium-1").mkdir(parents=True)
    (directory / "huggingface").mkdir()
    (directory / "ms-playwright" / "chromium-1" / "chrome").write_bytes(b"x" * 1000)
    (directory / "huggingface" / "model.bin").write_bytes(b"y" * 500)
    return 1500


# ---- the cache module -----------------------------------------------------------------


def test_cache_dir_is_one_component_under_the_data_dir(tmp_path):
    assert server_cache.cache_dir(tmp_path, "abc") == tmp_path.resolve() / "cache" / "abc"
    for bad in ("", ".", "..", "a/b", "../x", "a\\b", "a\0b"):
        with pytest.raises(ValueError):
            server_cache.cache_dir(tmp_path, bad)
        with pytest.raises(ValueError):
            server_cache.clear_cache(tmp_path, bad)
    # Delete's best-effort wrapper swallows the refusal rather than failing the delete.
    server_cache.remove_cache(tmp_path, "../x")


def test_clear_cache_deletes_the_directory_and_counts_its_bytes(tmp_path):
    directory = server_cache.cache_dir(tmp_path, "srv")
    directory.mkdir(parents=True)
    written = _fill(directory)
    freed = server_cache.clear_cache(tmp_path, "srv")
    assert written <= freed  # file bytes plus the directories' own entries
    assert not directory.exists()
    assert (tmp_path / "cache").is_dir()  # the root stays
    assert server_cache.clear_cache(tmp_path, "srv") == 0  # nothing left: a no-op
    assert server_cache.clear_cache(tmp_path, "never-existed") == 0


def test_clear_cache_never_follows_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("precious")
    root = tmp_path / "cache"
    root.mkdir()

    # A symlink planted as the cache directory itself is unlinked, its target untouched.
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert server_cache.clear_cache(tmp_path, "linked") == 0
    assert not (root / "linked").exists() and not (root / "linked").is_symlink()
    assert (outside / "keep").read_text() == "precious"

    # One inside the cache is removed as a link; the walk doesn't count or enter its target.
    inner = server_cache.cache_dir(tmp_path, "inner")
    inner.mkdir()
    (inner / "escape").symlink_to(outside, target_is_directory=True)
    (inner / "big").write_bytes(b"z" * 10)
    server_cache.clear_cache(tmp_path, "inner")
    assert not inner.exists()
    assert (outside / "keep").read_text() == "precious"


# ---- the env a local child sees -------------------------------------------------------


def test_child_env_points_caches_at_the_bridge_cache_dir(monkeypatch, tmp_path):
    cache = tmp_path / "cache" / "srv"
    monkeypatch.setenv(server_cache.BRIDGE_ENV_KEY, str(cache))
    monkeypatch.setenv("XDG_CACHE_HOME", "/data/.cache")  # the image-wide default
    monkeypatch.setenv("npm_config_cache", "/data/.npm-cache")
    child = _child_env({"env": {"GITHUB_TOKEN": "x"}})
    assert child["XDG_CACHE_HOME"] == str(cache)  # per-server beats the image default
    assert child["PLAYWRIGHT_BROWSERS_PATH"] == str(cache / "ms-playwright")
    assert child["npm_config_cache"] == "/data/.npm-cache"  # npm/uv stay shared
    assert server_cache.BRIDGE_ENV_KEY not in child  # the hand-off itself never leaks

    # The server's own env wins, key by key.
    child = _child_env({"env": {"XDG_CACHE_HOME": "/srv/mine"}})
    assert child["XDG_CACHE_HOME"] == "/srv/mine"
    assert child["PLAYWRIGHT_BROWSERS_PATH"] == str(cache / "ms-playwright")
    child = _child_env({"env": {"PLAYWRIGHT_BROWSERS_PATH": "0"}})
    assert child["PLAYWRIGHT_BROWSERS_PATH"] == "0"


def test_docker_child_gets_no_cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv(server_cache.BRIDGE_ENV_KEY, str(tmp_path))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    child = _child_env({"minimal_env": True, "env": {"A": "1"}})
    assert "XDG_CACHE_HOME" not in child
    assert "PLAYWRIGHT_BROWSERS_PATH" not in child


def _local(tmp_path: Path, **overrides) -> Server:
    values = dict(
        id="cache-unit", slug="cache-unit", name="Cache unit", runner="command",
        command=sys.executable, args=[str(FIXTURE)], env={}, config_hash="hash",
    )
    values.update(overrides)
    return Server(**values)


def test_unit_hands_the_cache_to_the_bridge_and_setup_only_for_local_runners(
    tmp_path, monkeypatch
):
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    # An inherited hand-off (the control plane's own env) never reaches a unit without one.
    monkeypatch.setenv(server_cache.BRIDGE_ENV_KEY, "/stale")
    expected = tmp_path.resolve() / "cache" / "cache-unit"

    unit = ServerUnit(_local(tmp_path, env={"PLAYWRIGHT_BROWSERS_PATH": "/opt/pw"}))
    unit.port = 12345
    payload = unit._bridge_payload()
    assert unit._bridge_env(payload)[server_cache.BRIDGE_ENV_KEY] == str(expected)
    # Not a spec field: the size-capped spec (and so every persisted one) is unchanged.
    assert str(expected) not in json.dumps(payload)
    setup = unit._effective_child_env()
    assert setup["XDG_CACHE_HOME"] == str(expected)
    assert setup["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw"  # the server's own env wins

    for runner, extra in (
        ("docker", {"command": "ghcr.io/x/y:1", "args": []}),
        ("remote", {"command": "https://up.example/mcp", "args": []}),
    ):
        other = ServerUnit(_local(tmp_path, runner=runner, **extra))
        other.port = 12345
        assert other.cache_dir is None
        assert server_cache.BRIDGE_ENV_KEY not in other._bridge_env(other._bridge_payload())


def test_cache_dir_leaves_config_hash_alone():
    """Derived from the id, never stored: an existing server's hash (and so its bridge)
    is untouched by the feature."""
    server = Server(id="h", slug="h", name="h", runner="command", command="echo", args=[], env={})
    payload = json.dumps(service._hash_payload(server), default=str)
    assert "cache" not in payload.lower()


async def test_setup_script_and_launched_server_share_the_cache(tmp_path, monkeypatch):
    """`npx playwright install` in a setup script must land where the server looks: the
    setup shell and the launched child (through the real bridge) see one directory."""
    from app.supervisor import unit as unit_module

    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    expected = tmp_path.resolve() / "cache" / "cache-unit"
    # The launch records what the child saw, then becomes the ordinary stdio fixture.
    launch = (
        'printf %s "$PLAYWRIGHT_BROWSERS_PATH" > "$XDG_CACHE_HOME/from-child"; '
        f'exec {sys.executable} {FIXTURE}'
    )
    server = _local(
        tmp_path,
        command="/bin/sh",
        args=["-c", launch],
        cwd=str(work),
        env={"EXPECTED_CWD": str(work)},
        setup_script='printf x > "$XDG_CACHE_HOME/from-setup"\ntouch setup-complete\n',
    )
    unit = ServerUnit(server)
    with socket_port() as port:
        await unit.start(port)
    try:
        async with asyncio.timeout(30):
            while unit.state not in ("running", "failed"):
                await asyncio.sleep(0.05)
        assert unit.state == "running", "\n".join(unit.logs.snapshot())
        assert (expected / "from-setup").read_text() == "x"
        assert (expected / "from-child").read_text() == str(expected / "ms-playwright")
    finally:
        await unit.stop()


class socket_port:
    def __enter__(self) -> int:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def __exit__(self, *exc) -> None:
        return None


# ---- the API ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(Supervisor, "run_forever", _parked)
    with TestClient(app) as c:
        yield c
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "auto", "bind_mode": "local"})
        s.execute(delete(Token))
        s.execute(delete(User))
        s.execute(delete(Server))
        s.commit()


def _create(client, headers=LOOPBACK, **fields) -> str:
    payload = {"name": "Cached", "runner": "command", "command": "/bin/true", **fields}
    r = client.post("/api/servers", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _cache(server_id: str) -> Path:
    return server_cache.cache_dir(get_settings().data_dir, server_id)


def test_clear_restarts_an_enabled_server_after_deleting_while_stopped(client, monkeypatch):
    server_id = _create(client)
    sup = client.app.state.supervisor
    directory = _cache(server_id)
    directory.mkdir(parents=True)
    written = _fill(directory)
    with Session(get_engine()) as session:
        server = service.set_enabled(session, server_id, True)
        before = (server.config_hash, server.updated_at)
        unit = ServerUnit(server)
        unit.state = "running"
        unit.port = 49996
        sup.units[server_id] = unit

    order: list[str] = []

    async def stop():
        # The bridge stops BEFORE the directory goes: nothing is writing into it.
        order.append("stop" if directory.exists() else "stop-after-delete")
        unit.state = "stopped"

    monkeypatch.setattr(unit, "stop", stop)
    restarts: list[str] = []
    real_restart = Supervisor.restart

    async def restart(self, sid, authorized=None, while_stopped=None):
        restarts.append(sid)
        return await real_restart(self, sid, authorized=authorized, while_stopped=while_stopped)

    monkeypatch.setattr(Supervisor, "restart", restart)

    r = client.post(f"/api/servers/{server_id}/cache/clear", headers=LOOPBACK)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["freed_bytes"] >= written
    assert body["restarted"] is True
    assert body["server"]["state"] == "starting"
    assert order == ["stop"]
    assert restarts == [server_id]  # through the one restart primitive
    assert not directory.exists()
    assert server_id not in sup.units
    assert sup.activation_requested_at(server_id) is not None
    with Session(get_engine()) as session:
        current = repo.get_server(session, server_id)
        assert (current.config_hash, current.updated_at) == before  # not an edit


def test_clear_on_a_disabled_server_only_deletes(client):
    server_id = _create(client)
    directory = _cache(server_id)
    directory.mkdir(parents=True)
    _fill(directory)
    r = client.post(f"/api/servers/{server_id}/cache/clear", headers=LOOPBACK)
    assert r.status_code == 200, r.text
    assert r.json()["restarted"] is False
    assert r.json()["freed_bytes"] >= 1500
    assert not directory.exists()
    assert client.app.state.supervisor.activation_requested_at(server_id) is None

    # Nothing cached yet is a clean no-op.
    r = client.post(f"/api/servers/{server_id}/cache/clear", headers=LOOPBACK)
    assert r.status_code == 200 and r.json()["freed_bytes"] == 0


def test_clear_refuses_runners_without_a_cache_and_unknown_servers(client):
    remote = _create(client, runner="remote", command="https://up.example/mcp")
    r = client.post(f"/api/servers/{remote}/cache/clear", headers=LOOPBACK)
    assert r.status_code == 400
    assert "local servers" in r.json()["detail"]
    assert client.post("/api/servers/ghost/cache/clear", headers=LOOPBACK).status_code == 404


def test_clear_404s_for_a_server_the_caller_cant_see(client):
    admin = {**LOOPBACK, "authorization": f"Bearer {mint_token('control')}"}
    with Session(get_engine()) as s:
        runtime_settings.write(s, {"control_plane_auth": "always"})
    user = client.post(
        "/api/users", json={"name": "Mel", "role": "member", "local_runners": True}, headers=admin
    ).json()
    cred = client.post(f"/api/users/{user['id']}/credentials", headers=admin).json()["token"]
    member = {**LOOPBACK, "authorization": f"Bearer {cred}"}

    theirs = _create(client, headers=admin)
    directory = _cache(theirs)
    directory.mkdir(parents=True)
    _fill(directory)
    r = client.post(f"/api/servers/{theirs}/cache/clear", headers=member)
    assert r.status_code == 404
    assert directory.exists()  # untouched

    mine = _create(client, headers=member)
    assert client.post(f"/api/servers/{mine}/cache/clear", headers=member).status_code == 200


def test_delete_removes_the_cache_directory(client):
    server_id = _create(client)
    directory = _cache(server_id)
    directory.mkdir(parents=True)
    _fill(directory)
    assert client.delete(f"/api/servers/{server_id}", headers=LOOPBACK).status_code == 204
    assert not directory.exists()


def test_control_mcp_clear_server_cache(client):
    from test_control_mcp import _call, _rpc

    with Session(get_engine()) as s:
        service.ensure_builtin(s)
    status, msg = _rpc(client, "tools/list")
    tool = next(t for t in msg["result"]["tools"] if t["name"] == "clear_server_cache")
    assert tool["annotations"]["destructiveHint"] is True
    assert "clear_server_cache" in _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    })[1]["result"].get("instructions", "")

    server_id = _create(client)
    directory = _cache(server_id)
    directory.mkdir(parents=True)
    _fill(directory)
    is_error, out = _call(client, "clear_server_cache", {"server_id": server_id})
    assert not is_error, out
    assert out["freed_bytes"] >= 1500 and out["restarted"] is False
    assert not directory.exists()
