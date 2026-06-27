"""Registry / SSOT tests — slug uniqueness and the config_hash idempotency anchor."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db import repo
from app.registry import service


@pytest.fixture
def session():
    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _mk(session, **kw):
    base = dict(name="Memory", runner="npx", command="npx", args=["-y", "pkg"])
    base.update(kw)
    return service.create_server(session, **base)


def test_unique_slugs(session):
    a = _mk(session, name="Memory", args=["-y", "x"])
    b = _mk(session, name="Memory", args=["-y", "y"])
    assert a.slug == "memory"
    assert b.slug == "memory-2"


def test_reserved_slug_is_not_assigned(session):
    """A server named "summary" must not get the slug "summary" — that would shadow
    the static /api/health/summary route so its own /api/health/{slug} is unreachable.
    It's disambiguated instead, leaving the reserved word free for the aggregate route."""
    a = _mk(session, name="summary")
    assert a.slug == "summary-2"
    # the reserved word stays free no matter how the name is cased/spaced
    b = _mk(session, name="Summary")
    assert b.slug == "summary-3"


def test_config_hash_changes_on_edit(session):
    a = _mk(session, args=["-y", "x"])
    before = a.config_hash
    service.update_server(session, a.id, {"args": ["-y", "z"]})
    after = repo.get_server(session, a.id).config_hash
    assert after != before


def test_config_hash_is_order_independent(session):
    """Same logical config -> same hash, regardless of env key order.

    This is the idempotency anchor: the reconciler must NOT restart a server when
    nothing meaningful changed.
    """
    a = _mk(session, env={"B": "2", "A": "1"})
    before = a.config_hash
    service.update_server(session, a.id, {"env": {"A": "1", "B": "2"}})
    after = repo.get_server(session, a.id).config_hash
    assert after == before


def test_unknown_runner_rejected(session):
    with pytest.raises(ValueError):
        service.create_server(session, name="x", runner="bogus", command="x")


def test_auth_provider_change_does_not_restart(session):
    """auth_provider is proxy-layer; changing it must NOT change config_hash
    (otherwise the reconciler would needlessly bounce the bridge)."""
    srv = _mk(session, args=["-y", "x"])
    before = srv.config_hash
    service.update_server(session, srv.id, {"auth_provider": "bearer"})
    assert repo.get_server(session, srv.id).config_hash == before
    # but a launch-affecting change still does
    service.update_server(session, srv.id, {"args": ["-y", "z"]})
    assert repo.get_server(session, srv.id).config_hash != before
