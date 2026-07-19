"""Shared pytest setup.

Pin ``MCPE_DATA_DIR`` to a throwaway directory BEFORE any ``app.*`` import caches
``get_settings()`` / ``get_engine()``, so a bare ``pytest`` run never reads or
writes a real data directory. An explicit ``MCPE_DATA_DIR`` still wins (setdefault).
"""

import os
import tempfile

os.environ.setdefault("MCPE_DATA_DIR", tempfile.mkdtemp(prefix="mcpe-tests-"))

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402  (the env pin above must precede app imports)

from app.db import get_engine, init_db, repo  # noqa: E402
from app.db.models import Token  # noqa: E402
from app.util import hash_token, new_id, new_token  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db_schema():
    """Create the schema before any test runs, so tests that touch the DB WITHOUT
    starting the app (e.g. oauth_flow unit tests, whose promotion guard reads the
    committed server row) work in any subset/order. Normally app startup does this;
    a standalone run of one test file would otherwise hit "no such table". A fixture
    rather than a module-level call keeps collection side-effect-free; idempotent
    against the throwaway (or explicitly pinned) data dir."""
    init_db()

LOOPBACK = {"host": "127.0.0.1"}  # passes the Host allowlist (TestClient peer is loopback)


def mint_token(scope: str = "all") -> str:
    """Insert a token of the given scope into the shared DB; return the plaintext."""
    raw = new_token()
    with Session(get_engine()) as session:
        repo.create_token(
            session,
            Token(id=new_id(), name=scope, token_hash=hash_token(raw), prefix=raw[:12], scope=scope),
        )
    return raw


def create_server(client, *, name: str, auth: str | None = None) -> dict:
    """Create a disabled server (no subprocess) via POST /api/servers; return its summary."""
    payload: dict = {"name": name, "command": "echo"}
    if auth is not None:
        payload["auth_provider"] = auth
    r = client.post("/api/servers", json=payload, headers=LOOPBACK)
    assert r.status_code == 201, r.text
    return r.json()
