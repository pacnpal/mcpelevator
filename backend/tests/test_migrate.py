"""init_db forward-migrates an older SQLite DB (ADD COLUMN) instead of crashing."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from app.db import _add_missing_columns
from app.db import models  # noqa: F401 — register tables on SQLModel.metadata


def test_add_missing_columns_backfills_new_column():
    eng = create_engine("sqlite://")
    # An older `server` table from before `auth_provider` existed (all else present).
    with eng.begin() as c:
        c.execute(
            text(
                """
                CREATE TABLE server (
                  id TEXT PRIMARY KEY, slug TEXT, name TEXT, runner TEXT, command TEXT,
                  args JSON, env JSON, cwd TEXT, mcp_http BOOLEAN, rest_openapi BOOLEAN,
                  enabled BOOLEAN, config_hash TEXT, source TEXT,
                  created_at DATETIME, updated_at DATETIME
                )
                """
            )
        )
        c.execute(
            text(
                "INSERT INTO server (id, slug, name, runner, command, args, env, mcp_http,"
                " rest_openapi, enabled, config_hash, source, created_at, updated_at)"
                " VALUES ('a','a','a','npx','npx','[]','{}',1,0,0,'','manual',"
                "'2026-01-01','2026-01-01')"
            )
        )

    _add_missing_columns(eng)  # the upgrade step

    cols = {c["name"] for c in inspect(eng).get_columns("server")}
    assert "auth_provider" in cols
    with eng.begin() as c:
        val = c.execute(text("SELECT auth_provider FROM server WHERE id='a'")).scalar()
    assert val == "inherit"  # the model default, backfilled onto the existing row


def test_add_missing_columns_is_idempotent():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)  # full current schema -> nothing missing
    _add_missing_columns(eng)
    _add_missing_columns(eng)  # running again is a no-op, not an error
