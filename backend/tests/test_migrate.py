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


def test_backfill_config_hashes_rehashes_stale_rows():
    from sqlmodel import Session

    from app.db import repo
    from app.registry import service

    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        srv = service.create_server(s, name="x", runner="npx", command="npx")
        current = srv.config_hash
        repo.set_config_hash(s, srv.id, "OLD-SHAPE")  # simulate an older hash-input shape
        assert service.backfill_config_hashes(s) == 1
        assert repo.get_server(s, srv.id).config_hash == current  # rehashed to current shape
        assert service.backfill_config_hashes(s) == 0  # idempotent — no further writes


def test_normalize_auth_providers_canonicalizes_legacy():
    from sqlalchemy import text
    from sqlmodel import Session

    from app.db import repo
    from app.registry import service

    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        a = service.create_server(s, name="a", runner="npx", command="npx", auth_provider="bearer")
        b = service.create_server(s, name="b", runner="npx", command="npx")
        # legacy free-text values the old `str` schema would have allowed
        s.execute(text("UPDATE server SET auth_provider='Bearer ' WHERE id=:i"), {"i": a.id})
        s.execute(text("UPDATE server SET auth_provider='basic' WHERE id=:i"), {"i": b.id})
        s.commit()
        assert service.normalize_auth_providers(s) == 2
        assert repo.get_server(s, a.id).auth_provider == "bearer"   # case/space canonicalized
        assert repo.get_server(s, b.id).auth_provider == "inherit"  # unresolvable -> default
        assert service.normalize_auth_providers(s) == 0  # idempotent
