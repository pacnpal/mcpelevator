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

Scope surgery: a request to ``/g/<name>/mcp`` arrives here behind the ``/g``
mount. ``root_path`` includes both an optional outer ``app_root_path`` and ``/g``,
while ``path`` may omit that app prefix when a proxy already stripped it. Deriving
the routing root relative to ``app_root_path``, then extending it to ``/g/<name>``,
makes the group's inner app (built with ``path="/mcp"``) resolve the route to
``/mcp`` in either deployment shape.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import get_route_path
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

        # A proxy may strip app_root_path from path while Starlette still accumulates it
        # into root_path. In that shape, route against only this app's mount portion
        # ("/g"); when path retains the outer prefix, keep the accumulated root instead.
        path = scope["path"]
        root_path = scope.get("root_path", "")
        app_root_path = scope.get("app_root_path", "")
        path_includes_app_root = path == app_root_path or path.startswith(
            f"{app_root_path}/"
        )
        routing_root_path = root_path
        if app_root_path and not path_includes_app_root:
            routing_root_path = root_path.removeprefix(app_root_path)

        route_scope = dict(scope)
        route_scope["root_path"] = routing_root_path
        route_path = get_route_path(route_scope)  # -> "/<name>/<rest>"
        name, _, _ = route_path.lstrip("/").partition("/")
        if not name:
            await Response("unknown group", status_code=404)(scope, receive, send)
            return

        request = Request(scope, receive)
        with Session(get_engine()) as session:
            known = registry.exists(session, name)
            member_ids = registry.resolve(session, name) if known else []
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

        # Authenticated group traffic counts as activity for every member, so a
        # running member serving through the bundle doesn't idle out underneath it.
        # (Group requests don't WAKE idle members — the bundle mounts running
        # members only, and remounting happens on the reconcile that follows a wake.)
        # app.state.supervisor is assigned in the lifespan before any request is
        # served, so access it directly — a missing attribute should fail fast.
        app = scope.get("app")
        if app is not None:
            for member_id in member_ids or []:
                app.state.supervisor.mark_activity(member_id)

        inner = self._hub.app_for(name)
        if inner is None:
            await Response("group not ready", status_code=503)(scope, receive, send)
            return

        # Delegate to the group's inner app. Extend the routing root by the group name
        # so the inner app (built with path="/mcp") resolves the remainder to "/mcp".
        # app_root_path remains on the scope for external URL generation.
        sub_scope = dict(scope)
        sub_scope["root_path"] = routing_root_path + "/" + name
        await inner(sub_scope, receive, send)
