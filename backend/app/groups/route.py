"""ASGI dispatcher for group endpoints, mounted at ``/g``.

Registered BEFORE the SPA catch-all mount (registration order wins). The group name
is dynamic — the first path segment after ``/g`` — so this parses it and looks it up
in the registry. The gate sequence mirrors the per-server proxy exactly: existence
(is ``<name>`` a registered group?), then ``enforce()`` (Host/Origin allowlist +
per-group auth), then liveness — before delegating to the group's current FastMCP app.

Deterministic behavior:

- **Unknown group name** -> 404 with the same body the ``/s`` proxy returns for
  an unknown slug ("unknown group"). Never a 500.
- **Known group, nothing built yet** (transient during startup/swap) -> 503.
- **Known but empty group** (no running members) -> the hub still builds a valid
  (tool-less) bundle, so ``initialize`` succeeds and ``tools/list`` is ``[]``.

Scope surgery: a request to ``/g/<name>/mcp`` arrives here (behind the ``/g``
mount) with ``root_path == "/g"`` and ``path == "/g/<name>/mcp"``. The group's
inner app was built with ``path="/mcp"``, so extending ``root_path`` to
``/g/<name>`` makes Starlette's ``get_route_path`` (path minus root_path) resolve
to ``/mcp``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from app.auth.middleware import enforce
from app.db import get_engine
from app.groups import registry
from app.groups.hub import GroupHub, group_server


class GroupDispatch:
    def __init__(self, hub: GroupHub) -> None:
        self._hub = hub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover — Mount only routes http here
            raise RuntimeError("GroupDispatch only handles http")

        root_path = scope.get("root_path", "")
        route_path = scope["path"][len(root_path):]  # "/g" stripped -> "/<name>/<rest>"
        name, _, _ = route_path.lstrip("/").partition("/")
        if not name:
            await Response("unknown group", status_code=404)(scope, receive, send)
            return

        request = Request(scope, receive)
        with Session(get_engine()) as session:
            known = registry.exists(session, name)
        if not known:
            # indistinguishable from a nonexistent slug (same shape as the proxy's 404)
            await Response("unknown group", status_code=404)(scope, receive, send)
            return

        try:
            await enforce(request, group_server(name))
        except HTTPException as exc:
            # raw ASGI — render the exception the way FastAPI's handler would,
            # preserving WWW-Authenticate etc.
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
            await response(scope, receive, send)
            return

        inner = self._hub.app_for(name)
        if inner is None:
            await Response("group not ready", status_code=503)(scope, receive, send)
            return

        # Delegate to the group's inner app. Extend root_path by the group name so the
        # inner app (built with path="/mcp") resolves the route path to "/mcp"; path
        # stays the full "/g/<name>/mcp" (Starlette routes on path minus root_path).
        sub_scope = dict(scope)
        sub_scope["root_path"] = root_path + "/" + name
        await inner(sub_scope, receive, send)
