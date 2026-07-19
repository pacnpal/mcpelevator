"""SQLite engine + session helpers. SQLite is the single source of truth (SSOT)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.resolved_db_path}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    # Import models so their tables register on SQLModel.metadata before create_all.
    from app.db import models  # noqa: F401

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _create_missing_indexes(engine)


def _sql_literal(value: Any) -> str:
    """Render a Python default as a SQLite literal for ``ADD COLUMN ... DEFAULT``."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return "'" + json.dumps(value).replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _column_default(col) -> str:
    """A SQL default for a newly added column so existing rows get a value. Prefer
    the model's scalar default; otherwise NULL (nullable) or a type-appropriate zero."""
    default = col.default
    if default is not None and getattr(default, "is_scalar", False):
        return _sql_literal(default.arg)
    if col.nullable:
        return "NULL"
    if isinstance(col.type, JSON):
        return "'[]'"
    if isinstance(col.type, DateTime):
        return "CURRENT_TIMESTAMP"
    if isinstance(col.type, (Integer, Boolean)):
        return "0"
    return "''"


def _add_missing_columns(engine) -> None:
    """Forward-only migration: ``ADD COLUMN`` for any model column absent from an
    existing table. ``SQLModel.create_all`` creates missing tables but never ALTERs
    existing ones, so without this an upgrade over an older DB crashes the first time
    a query touches a new column. Idempotent — adds only what's missing, with a
    default so existing rows upgrade cleanly. (Forward-only is enough for SSOT here;
    a real migration tool is deferred until a column needs a non-trivial backfill.)"""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(engine.dialect)
                not_null = " NOT NULL" if not col.nullable else ""
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                        f"{col_type}{not_null} DEFAULT {_column_default(col)}"
                    )
                )


def _create_missing_indexes(engine) -> None:
    """Forward-only companion to ``_add_missing_columns``: ``create_all`` builds
    indexes only for tables IT creates, so a model index declared on a column that
    reaches an existing table via ADD COLUMN (e.g. ``server.owner_id``) would never
    materialize on upgraded databases. ``checkfirst`` makes this idempotent."""
    for table in SQLModel.metadata.sorted_tables:
        for index in table.indexes:
            index.create(engine, checkfirst=True)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
