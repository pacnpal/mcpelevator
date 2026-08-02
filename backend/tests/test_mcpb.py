"""MCPB export — the generated bundle mirrors the launch spec; remote servers 400."""

from __future__ import annotations

import io
import json
import zipfile

from fastapi.testclient import TestClient

from conftest import LOOPBACK

from app import __version__
from app.main import app


def _manifest_from(body: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return json.loads(zf.read("manifest.json"))


def test_mcpb_download_round_trip():
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
            base = __version__.lstrip("v").split("+", 1)[0]
            assert m["version"] == f"{base}+{detail['config_hash']}"
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


def test_mcpb_rejects_remote_servers():
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
