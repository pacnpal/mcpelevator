"""Supervisor reconcile tests — the parts that don't spawn real bridge processes.

Focus: a slug rename must converge onto a running unit's in-memory routing key.
slug is excluded from ``config_hash`` (a rename must not bounce the bridge), so the
reconciler — not a restart — is what keeps a live unit's ``slug`` in sync with
desired state. This guards the race where ``rename_slug`` missed a unit that didn't
exist yet and was then started from a pre-rename snapshot.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlmodel import Session

from app.db import get_engine, init_db, repo
from app.registry import service
from app.supervisor.supervisor import Supervisor

init_db()  # ensure the global-engine tables exist when this module runs alone


def _fake_unit(server) -> SimpleNamespace:
    """A stand-in for a live ServerUnit carrying only what reconcile reads/writes."""
    return SimpleNamespace(
        slug=server.slug,
        config_hash=server.config_hash,
        state="running",
        pid=1234,
        port=9999,
        last_error=None,
        tools=[],
    )


async def test_reconcile_converges_renamed_slug_onto_live_unit():
    sup = Supervisor()
    with Session(get_engine()) as session:
        server = service.create_server(
            session, name="Conv", runner="npx", command="npx", args=["-y", "x"], enabled=True
        )
    sid = server.id
    try:
        # A live unit pinned to the OLD slug (as if rename_slug missed it).
        unit = _fake_unit(server)
        unit.slug = "stale-slug"
        sup.units[sid] = unit

        # Desired state now carries the renamed slug.
        with Session(get_engine()) as session:
            service.update_server(session, sid, {"slug": "fresh-slug"})

        await sup.reconcile_once()

        # The reconciler copied the fresh slug onto the live unit (no restart:
        # same config_hash means the unit object is unchanged, only its slug).
        assert sup.units[sid] is unit
        assert unit.slug == "fresh-slug"
    finally:
        sup.units.pop(sid, None)
        with Session(get_engine()) as session:
            repo.delete_server(session, sid)
