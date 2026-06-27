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


def test_remote_server_canonicalizes_and_validates(session):
    s = service.create_server(
        session,
        name="Remote",
        runner="remote",
        command="https://up.example/mcp",
        args=["http"],  # alias → canonical streamable-http
        env={"Authorization": "Bearer t"},
    )
    assert s.runner == "remote"
    assert s.command == "https://up.example/mcp"
    assert s.args == ["streamable-http"]  # canonicalized for deterministic storage
    assert s.env == {"Authorization": "Bearer t"}


def test_remote_server_defaults_transport(session):
    s = service.create_server(session, name="R", runner="remote", command="https://x/mcp")
    assert s.args == ["streamable-http"]


def test_remote_server_rejects_non_url(session):
    with pytest.raises(ValueError):
        service.create_server(session, name="R", runner="remote", command="not-a-url")


def test_remote_server_accepts_uppercase_scheme(session):
    # URL schemes are case-insensitive — HTTPS:// must not be rejected.
    s = service.create_server(session, name="R", runner="remote", command="HTTPS://x/mcp")
    assert s.runner == "remote"


def test_remote_server_rejects_bad_transport(session):
    with pytest.raises(ValueError):
        service.create_server(
            session, name="R", runner="remote", command="https://x/mcp", args=["websocket"]
        )


def test_remote_config_hash_is_deterministic(session):
    """Same logical remote config (alias-normalized) → same hash; a transport change
    re-hashes (drives one idempotent reconcile)."""
    a = service.create_server(
        session, name="A", runner="remote", command="https://x/mcp", args=["http"]
    )
    b = service.create_server(
        session, name="B", runner="remote", command="https://x/mcp", args=["streamable-http"]
    )
    assert a.config_hash == b.config_hash  # "http" alias collapses to the same spec
    before = a.config_hash
    service.update_server(session, a.id, {"args": ["sse"]})
    assert repo.get_server(session, a.id).config_hash != before


def test_slug_rename(session):
    a = _mk(session, name="Memory")
    assert a.slug == "memory"
    service.update_server(session, a.id, {"slug": "brain"})
    assert repo.get_server(session, a.id).slug == "brain"
    # the freed slug can now be reused by another server
    b = _mk(session, name="Memory")
    assert b.slug == "memory"


def test_slug_rename_is_normalized(session):
    a = _mk(session, name="Memory")
    service.update_server(session, a.id, {"slug": "My Cool Server!!"})
    assert repo.get_server(session, a.id).slug == "my-cool-server"


def test_slug_rename_rejects_collision(session):
    a = _mk(session, name="Alpha", args=["-y", "a"])
    _mk(session, name="Beta", args=["-y", "b"])
    with pytest.raises(ValueError):
        service.update_server(session, a.id, {"slug": "beta"})


def test_slug_rename_to_self_is_allowed(session):
    a = _mk(session, name="Memory")
    service.update_server(session, a.id, {"slug": "memory"})
    assert repo.get_server(session, a.id).slug == "memory"


def test_slug_rename_rejects_reserved(session):
    a = _mk(session, name="Memory")
    with pytest.raises(ValueError):
        service.update_server(session, a.id, {"slug": "summary"})


def test_slug_rename_does_not_restart(session):
    """Slug is routing/identity, not launch config — renaming it must not change
    config_hash (which would needlessly bounce the bridge)."""
    a = _mk(session, args=["-y", "x"])
    before = a.config_hash
    service.update_server(session, a.id, {"slug": "renamed"})
    assert repo.get_server(session, a.id).config_hash == before


def test_clone_server_copies_config(session):
    src = _mk(session, name="Memory", env={"K": "v"}, args=["-y", "pkg"])
    src = service.update_server(session, src.id, {"auth_provider": "bearer"})

    copy = service.clone_server(session, src.id)
    assert copy.id != src.id
    assert copy.slug != src.slug  # unique slug derived from the new name
    assert copy.name == "Memory copy"
    assert copy.runner == src.runner
    assert copy.command == src.command
    assert copy.args == src.args
    assert copy.env == src.env
    assert copy.auth_provider == src.auth_provider
    assert copy.enabled is False  # always created disabled
    assert copy.source == "clone"
    assert copy.config_hash == src.config_hash  # identical launch config -> same hash


def test_clone_server_custom_name(session):
    src = _mk(session, name="Memory")
    copy = service.clone_server(session, src.id, name="Memory (staging)")
    assert copy.name == "Memory (staging)"
    assert copy.slug == "memory-staging"


def test_clone_unknown_server(session):
    with pytest.raises(KeyError):
        service.clone_server(session, "nope")


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
