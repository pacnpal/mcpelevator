"""Import tests — mcpServers JSON → servers, with runner inference and skips."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.registry import service


@pytest.fixture
def session():
    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_import_creates_stdio_and_remote(session):
    data = {
        "mcpServers": {
            "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
            "time": {"command": "uvx", "args": ["mcp-server-time"]},
            "remote": {
                "type": "streamable-http",
                "url": "https://x/mcp",
                "headers": {"Authorization": "Bearer t"},
            },
            "nocmd": {"foo": "bar"},
        }
    }
    created, skipped = service.import_mcp_servers(session, data)

    by_name = {c.name: c for c in created}
    assert set(by_name) == {"memory", "time", "remote"}
    assert by_name["memory"].runner == "npx"
    assert by_name["time"].runner == "uvx"
    assert all(c.enabled is False and c.source == "import" for c in created)
    # stored verbatim (mcpServers round-trip)
    assert by_name["memory"].command == "npx"
    assert by_name["memory"].args == ["-y", "@modelcontextprotocol/server-memory"]
    # remote entry → a "remote" runner proxying the upstream URL, transport in args,
    # headers in env.
    rem = by_name["remote"]
    assert rem.runner == "remote"
    assert rem.command == "https://x/mcp"
    assert rem.args == ["streamable-http"]
    assert rem.env == {"Authorization": "Bearer t"}

    reasons = {s["name"]: s["reason"] for s in skipped}
    assert set(reasons) == {"nocmd"}


def test_import_remote_honors_transport_field_alias(session):
    # An entry may spell the transport as `transport` instead of `type`; an SSE-only
    # upstream must not be silently imported as streamable-http.
    data = {"mcpServers": {"r": {"url": "https://up.example/sse", "transport": "sse"}}}
    created, skipped = service.import_mcp_servers(session, data)
    assert skipped == []
    assert created[0].runner == "remote"
    assert created[0].args == ["sse"]


def test_import_remote_honors_gemini_http_url(session):
    # Gemini CLI (and our own install snippets) use `httpUrl` for Streamable HTTP.
    data = {"mcpServers": {"r": {"httpUrl": "https://up.example/mcp"}}}
    created, skipped = service.import_mcp_servers(session, data)
    assert skipped == []
    assert created[0].runner == "remote"
    assert created[0].command == "https://up.example/mcp"
    assert created[0].args == ["streamable-http"]


def test_import_skips_malformed_entries_without_crashing(session):
    # A non-mapping `headers` makes dict() raise TypeError; the import must skip the
    # entry (not 500) and still create the valid ones.
    data = {
        "mcpServers": {
            "ok": {"command": "npx", "args": []},
            "badheaders": {"url": "https://x/mcp", "headers": 5},
            "badenv": {"command": "npx", "env": "nope"},
        }
    }
    created, skipped = service.import_mcp_servers(session, data)
    assert {c.name for c in created} == {"ok"}
    assert {s["name"] for s in skipped} == {"badheaders", "badenv"}


def test_import_accepts_bare_map(session):
    created, skipped = service.import_mcp_servers(session, {"m": {"command": "npx", "args": []}})
    assert len(created) == 1 and created[0].runner == "npx"
    assert skipped == []


def test_import_rejects_non_mapping(session):
    with pytest.raises(ValueError):
        service.import_mcp_servers(session, {"mcpServers": []})


def test_import_docker_entry_normalizes_to_canonical_shape(session):
    # The flagship github-mcp-server config: an absolute docker path + a full `run …`
    # invocation must import as a disabled docker server stored in canonical shape
    # (command = image, args = container args, env = the token).
    data = {
        "mcpServers": {
            "github": {
                "command": "/usr/local/bin/docker",
                "args": [
                    "run", "-i", "--rm",
                    "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                    "ghcr.io/github/github-mcp-server",
                ],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "pat"},
            }
        }
    }
    created, skipped = service.import_mcp_servers(session, data)
    assert skipped == []
    gh = created[0]
    assert gh.runner == "docker"  # inferred from the absolute docker path (basename)
    assert gh.command == "ghcr.io/github/github-mcp-server"
    assert gh.args == []
    assert gh.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "pat"}
    assert gh.enabled is False  # imported disabled — the root-equivalent gate bites on enable
