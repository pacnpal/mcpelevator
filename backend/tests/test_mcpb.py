"""MCPB export — the generated bundle mirrors the launch spec; remote servers 400."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import jsonschema
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from conftest import LOOPBACK

from app import __version__, mcpb, sealed
from app.db import get_engine, repo
from app.db.models import Server
from app.main import app
from app.runners.docker import server_label
from app.supervisor.supervisor import Supervisor

# The official manifest schema, verbatim from @anthropic-ai/mcpb 2.1.2 (MIT):
# dist/mcpb-manifest-v0.2.schema.json — the same zod shape Claude Desktop applies
# (strictly: unknown keys reject the install) before it will install a bundle.
OFFICIAL_SCHEMA = mcpb.SCHEMA_PATH


def _manifest_from(body: bytes) -> dict:
    """The parsed ``manifest.json`` inside a downloaded ``.mcpb`` zip."""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return json.loads(zf.read("manifest.json"))


def test_mcpb_download_round_trip():
    """The endpoint serves a zip whose manifest mirrors the row's launch spec."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Everything",
                "runner": "npx",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-everything"],
                "env": {"FOO": "bar"},
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server = created.json()
        try:
            r = c.get(f"/api/servers/{server['id']}/mcpb", headers=LOOPBACK)
            assert r.status_code == 200
            assert r.headers["content-disposition"] == (
                f'attachment; filename="{server["slug"]}.mcpb"'
            )
            m = _manifest_from(r.content)
            assert m["manifest_version"] == "0.2"
            assert m["name"] == server["id"]  # immutable identity, not the renameable slug
            assert m["display_name"] == "Everything"
            detail = c.get(f"/api/servers/{server['id']}", headers=LOOPBACK).json()
            assert m["version"] == f"{mcpb._semver(__version__)}+{detail['config_hash']}"
            assert detail["mcpb_exportable"] is True
            assert m["server"]["mcp_config"] == {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-everything"],
                "env": {"FOO": "bar"},
            }
        finally:
            c.delete(f"/api/servers/{server['id']}", headers=LOOPBACK)


def test_mcpb_rejects_unexportable_launch_context():
    """cwd/setup_script have no MCPB equivalent — refuse rather than hand out a
    bundle that can't reproduce the server."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Prepared",
                "runner": "command",
                "command": "/bin/true",
                "setup_script": "printf 'ready\\n'\n",
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            r = c.get(f"/api/servers/{server_id}/mcpb", headers=LOOPBACK)
            assert r.status_code == 400
            assert "setup script" in r.json()["detail"]
            # The detail response advertises the same verdict the endpoint enforces.
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK).json()
            assert detail["mcpb_exportable"] is False
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def test_mcpb_rejects_relative_command_paths():
    """A ``./server``-style command resolves only inside the elevator's cwd — 400."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Local build", "runner": "command", "command": "./server"},
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            r = c.get(f"/api/servers/{server_id}/mcpb", headers=LOOPBACK)
            assert r.status_code == 400
            assert "relative path" in r.json()["detail"]
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


def _row(runner: str, command: str, **kw) -> Server:
    """An unsaved ``Server`` row for exercising ``mcpb.manifest`` without the API."""
    return Server(id="x", slug="s", name="S", runner=runner, command=command,
                  args=kw.pop("args", []), env=kw.pop("env", {}), **kw)


def test_mcpb_command_path_classification():
    """Both path flavors: relative rejects, PATH-names and absolute paths export."""

    def row(command: str) -> Server:
        """A ``command``-runner row launching ``command``."""
        return _row("command", command)

    for cmd in (r".\server.exe", r"bin\server.exe", "./server", "bin/server"):
        try:
            mcpb.manifest(row(cmd))
            raise AssertionError(f"{cmd!r} should have been rejected")
        except ValueError as exc:
            assert "relative path" in str(exc)
    for cmd in ("npx", "/usr/local/bin/server", r"C:\tools\server.exe", r"\\host\share\server.exe"):
        assert mcpb.manifest(row(cmd))["server"]["mcp_config"]["command"] == cmd


def test_mcpb_rejects_remote_servers():
    """A remote server has nothing to run locally — 400."""
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Upstream", "runner": "remote", "command": "https://up.example/mcp"},
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        try:
            r = c.get(f"/api/servers/{server_id}/mcpb", headers=LOOPBACK)
            assert r.status_code == 400
            assert "stdio" in r.json()["detail"]
        finally:
            c.delete(f"/api/servers/{server_id}", headers=LOOPBACK)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.6.0", "1.6.0"),
        ("v1.6.0", "1.6.0"),
        ("1.7.0rc1", "1.7.0"),  # PEP 440 pre-release from pyproject
        ("1.7.0.dev0", "1.7.0"),
        ("0.0.0+unknown", "0.0.0"),  # app.__init__ fallback
        ("2024.03.31.01", "2024.3.31"),  # the anthropics/mcpb#226 crash string
        ("garbage", "0.0.0"),
        ("1.7.0-rc.1", "1.7.0-rc.1"),  # a release-workflow prerelease tag survives
        ("v1.7.0-beta.2+meta", "1.7.0-beta.2"),
        ("1.7.0-rc.01", "1.7.0"),  # numeric prerelease id with a leading zero is invalid
        ("1.7.0-", "1.7.0"),
        ("9007199254740991.0.0", "9007199254740991.0.0"),  # node-semver's ceiling
        ("9007199254740992.0.0", "0.0.0"),  # one past it
    ],
)
def test_mcpb_version_is_strict_semver(monkeypatch, raw, expected):
    """Claude Desktop crashes on every launch once a bundle with a non-semver
    version is installed (anthropics/mcpb#226) — the core is always M.m.p within
    node-semver's bounds, and only an already-valid prerelease is carried."""
    monkeypatch.setattr("app.mcpb.__version__", raw)
    row = _row("command", "/bin/true", config_hash="ab12.0123456789abcdef")
    assert mcpb.manifest(row)["version"] == f"{expected}+ab12.0123456789abcdef"
    row.config_hash = ""
    assert mcpb.manifest(row)["version"] == expected


def test_mcpb_docker_bundle_drops_reaping_label():
    """The elevator ``docker rm -f``s every container carrying its label for a
    server it owns (boot orphan sweep, unit stop). A bundle run against the same
    daemon must not wear that mark — everything else in the hardened argv stays."""
    row = _row("docker", "ghcr.io/x/y:1", args=["--flag"], env={"TOKEN": "t"})
    cfg = mcpb.manifest(row)["server"]["mcp_config"]
    assert cfg["command"] == "docker"
    assert "--label" not in cfg["args"]
    assert server_label(row.id) not in cfg["args"]
    assert cfg["args"][:3] == ["run", "-i", "--rm"]
    assert cfg["args"][-3:] == ["--", "ghcr.io/x/y:1", "--flag"]
    assert "-e" in cfg["args"] and cfg["args"][cfg["args"].index("-e") + 1] == "TOKEN"
    assert cfg["env"] == {"TOKEN": "t"}


def test_mcpb_manifest_matches_official_schema():
    """Every exportable runner shape validates against the official 0.2 schema —
    the same strict shape Claude Desktop checks at install."""
    validator = jsonschema.Draft7Validator(json.loads(OFFICIAL_SCHEMA.read_text()))
    rows = [
        _row("npx", "npx", args=["-y", "@modelcontextprotocol/server-everything"], env={"FOO": "bar"}),
        _row("uvx", "uvx", args=["mcp-server-fetch"], pin_mcp1=True),
        _row("command", "/usr/local/bin/server"),
        _row("docker", "ghcr.io/x/y:1", env={"TOKEN": "t"}),
        _row("npx", "npx", args=["-y", "pkg"], env={"KEY": sealed.seal("v"), "MODE": "fast"}),
    ]
    for row in rows:
        errors = list(validator.iter_errors(mcpb.manifest(row)))
        assert not errors, (row.runner, [e.message for e in errors])


def test_mcpb_secret_env_becomes_sensitive_prompts():
    """A secret value isn't readable on the detail route, so the bundle doesn't carry
    it either: it's a required, sensitive install-time prompt. Plain values stay."""
    row = _row("npx", "npx", args=["-y", "pkg"],
               env={"API_KEY": sealed.seal("s3cret"), "MODE": "fast"})
    out = mcpb.manifest(row)
    assert "s3cret" not in json.dumps(out)
    assert out["server"]["mcp_config"]["env"] == {
        "API_KEY": "${user_config.env_0}",
        "MODE": "fast",
    }
    prompt = out["user_config"]["env_0"]
    assert prompt["title"] == "API_KEY" and prompt["sensitive"] and prompt["required"]


def test_mcpb_lists_cached_tools_under_upstream_names():
    """Renamed tools appear under their upstream name (the bundle runs the upstream
    without mcpelevator's renames), and an operator's description override stays out."""
    row = _row("npx", "npx", args=["-y", "pkg"],
               tool_overrides={"search": {"name": "find", "description": "mine"}})
    tools = [
        {"name": "find", "upstream_name": "search", "description": "mine"},
        {"name": "fetch", "description": "Fetch a URL"},
    ]
    out = mcpb.manifest(row, tools)
    assert out["tools"] == [{"name": "search"}, {"name": "fetch", "description": "Fetch a URL"}]
    assert out["tools_generated"] is True


def test_mcpb_absolute_command_pins_its_platform():
    assert mcpb.manifest(_row("command", "/usr/local/bin/s"))["compatibility"] == {
        "platforms": ["darwin", "linux"]
    }
    assert mcpb.manifest(_row("command", "C:\\tools\\s.exe"))["compatibility"] == {
        "platforms": ["win32"]
    }
    assert "compatibility" not in mcpb.manifest(_row("npx", "npx", args=["-y", "pkg"]))


def test_mcpb_refuses_a_manifest_the_schema_rejects(monkeypatch):
    """Validated on every export: a manifest the official schema rejects is never served."""
    monkeypatch.setattr(mcpb, "MANIFEST_VERSION", "9.9")
    row = _row("npx", "npx", args=["-y", "pkg"])
    with pytest.raises(ValueError, match="manifest_version"):
        mcpb.manifest(row)
    assert not mcpb.exportable(row)


def test_mcpb_tools_only_from_current_launch_spec(monkeypatch):
    """The cached tool list ships only while it was probed under the row's current
    config_hash: after a command edit it describes the old executable (#206)."""

    async def parked_reconciler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Supervisor, "run_forever", parked_reconciler)
    tools = [{"name": "echo", "description": "Echo back", "has_output_schema": False}]
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={"name": "Tools", "runner": "command", "command": "/bin/true"},
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server = created.json()
        url = f"/api/servers/{server['id']}/mcpb"

        def stamp() -> None:
            """Cache ``tools`` as probed under the row's current config_hash."""
            with Session(get_engine()) as session:
                row = repo.get_server(session, server["id"])
                repo.upsert_runtime(
                    session, row.id, tools=tools, tools_config_hash=row.config_hash
                )

        try:
            stamp()
            assert _manifest_from(c.get(url, headers=LOOPBACK).content)["tools"] == [
                {"name": "echo", "description": "Echo back"}
            ]
            edited = c.patch(
                f"/api/servers/{server['id']}",
                json={"command": "/bin/false"},
                headers=LOOPBACK,
            )
            assert edited.status_code == 200, edited.text
            assert "tools" not in _manifest_from(c.get(url, headers=LOOPBACK).content)
            stamp()
            assert _manifest_from(c.get(url, headers=LOOPBACK).content)["tools"]
        finally:
            c.delete(f"/api/servers/{server['id']}", headers=LOOPBACK)
