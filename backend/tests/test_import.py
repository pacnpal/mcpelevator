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
