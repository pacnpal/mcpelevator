"""Shared pytest setup.

Pin ``MCPE_DATA_DIR`` to a throwaway directory BEFORE any ``app.*`` import caches
``get_settings()`` / ``get_engine()``, so a bare ``pytest`` run never reads or
writes a real data directory. An explicit ``MCPE_DATA_DIR`` still wins (setdefault).
"""

import os
import tempfile

os.environ.setdefault("MCPE_DATA_DIR", tempfile.mkdtemp(prefix="mcpe-tests-"))

from sqlmodel import Session  # noqa: E402  (the env pin above must precede app imports)

from app.db import get_engine, repo  # noqa: E402
from app.db.models import Token  # noqa: E402
from app.util import hash_token, new_id, new_token  # noqa: E402

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
