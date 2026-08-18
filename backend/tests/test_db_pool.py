"""Long-lived requests must not starve the control plane of DB connections.

A request holds its ``Depends(get_session)`` session until the response is fully
sent, so an SSE log stream held one for as long as the viewer kept the tab open.
Under SQLAlchemy's default QueuePool (5 + 10 overflow) enough open streams parked
every connection, and the next /api request — login included — blocked 30s in the
Host/Origin allowlist middleware and 500'd with ``QueuePool limit ... reached``.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

from sqlalchemy import event, select
from sqlmodel import Session
from starlette.requests import Request

from app.api import servers as servers_api
from app.auth.principal import LOCAL_ADMIN
from app.db import get_engine, repo
from app.db.models import Server, Setting
from app.supervisor.logbuffer import LogBuffer
from app.util import new_id

CONCURRENT = 40  # far past the old 5 + 10 ceiling


def test_many_simultaneous_sessions_never_queue_for_a_connection():
    """The old pool blocked the 16th caller for 30s, then raised TimeoutError."""
    engine = get_engine()
    started = time.monotonic()
    with ExitStack() as sessions:
        for _ in range(CONCURRENT):
            # Run a statement: a Session takes a connection lazily, on first use.
            sessions.enter_context(Session(engine)).exec(select(Setting).limit(1))
    assert time.monotonic() - started < 5.0


@contextmanager
def _open_connections():
    """Yield a callable reporting how many DB connections are open right now."""
    engine = get_engine()
    count = 0

    @event.listens_for(engine, "connect")
    def _opened(*_args):
        nonlocal count
        count += 1

    @event.listens_for(engine, "close")
    def _closed(*_args):
        nonlocal count
        count -= 1

    try:
        yield lambda: count
    finally:
        event.remove(engine, "connect", _opened)
        event.remove(engine, "close", _closed)


async def test_log_stream_hands_its_connection_back_before_streaming():
    """The SSE handler holds a connection only for its entry checks — the stream
    itself may then sit idle for hours without one."""
    unit = SimpleNamespace(state="running", logs=LogBuffer())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/servers/x/logs",
            "headers": [],
            "app": SimpleNamespace(
                state=SimpleNamespace(supervisor=SimpleNamespace(unit=lambda _id: unit))
            ),
        }
    )
    with Session(get_engine()) as setup:
        server = repo.create_server(
            setup,
            Server(
                id=new_id(), slug=f"logstream-{new_id()}", name="log stream",
                runner="command", command="echo", args=[], env={},
            ),
        )
    try:
        with _open_connections() as open_now, Session(get_engine()) as session:
            resp = await servers_api.stream_logs(
                server.id, request, session=session, principal=LOCAL_ADMIN
            )
            assert resp.status_code == 200
            assert open_now() == 0, "the request session still holds a connection"
    finally:
        with Session(get_engine()) as cleanup:
            repo.delete_server(cleanup, server.id)
