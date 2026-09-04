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

A tool called through a bundle is counted against the member that owns it, so
group traffic shows up in the same per-server / per-tool usage as a direct ``/s``
call (see :func:`_record_group_usage`).

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

from app import usage
from app.auth.middleware import enforce
from app.db import get_engine
from app.groups import registry
from app.groups.hub import GroupHub, group_server
from app.usage import attribution


async def _record_group_usage(
    request: Request, receive: Receive, mounted: dict[str, str]
) -> Receive:
    """Count a tool called through the bundle against the MEMBER that owns it,
    and return the receive channel the inner app should read.

    A tool reached through a group is the same tool call a direct ``/s`` request
    would make, so it belongs in the same counters — the hub namespaces tools by
    slug (``<slug>_<tool>``), which is exactly the attribution this needs. Non-tool
    group traffic (``initialize``, ``tools/list``) is deliberately NOT counted: it
    fans out to every member, and charging each one would invent traffic none of
    them individually received.

    Attribution is resolved against the members the hub is CURRENTLY serving, not
    the group's configured membership: a member that is stopped, has `mcp_http`
    off, or is excluded by the anti-downgrade rule is in the registry but has no
    provider in the bundle, so a call naming its namespace gets a tool-not-found —
    crediting it would let any caller inflate a server the request never reached.

    `mounted` carries the server id with the slug, so this stays off the database:
    resolving a namespace by loading every registered server would put O(servers)
    of synchronous ORM work on the event loop for every group call, in the one
    place the design keeps deliberately in-memory.

    Reading the body consumes the ASGI stream, so the buffered bytes are replayed
    to the inner app. A body of unknown length (chunked) or above the parse cap is
    left untouched — usage accounting never buffers a body it can't bound, and
    never changes what the group serves."""
    if request.method != "POST" or not mounted:
        return receive
    raw_length = request.headers.get("content-length")
    try:
        length = int(raw_length) if raw_length is not None else -1
    except ValueError:
        length = -1
    if length < 0 or length > attribution.MAX_PARSE_BYTES:
        return receive

    try:
        body = await request.body()
    except Exception:
        # The client went away (or the body couldn't be read). Nothing to count —
        # hand back the real channel and let the inner app see the disconnect it
        # would have seen without us.
        return receive
    replayed = False

    async def replay():
        # One buffered http.request, then back to the real channel so the inner
        # app still sees http.disconnect if the client goes away mid-call.
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    for name in attribution.tools_from_body(body):
        hit = attribution.split_namespaced(name, mounted)
        if hit is not None:
            usage.record(mounted[hit[0]], [hit[1]])
    return replay


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
        name, _, subpath = route_path.lstrip("/").partition("/")
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

        inner = self._hub.app_for(name)
        if inner is None:
            await Response("group not ready", status_code=503)(scope, receive, send)
            return

        # Delegate to the group's inner app. Extend the routing root by the group name
        # so the inner app (built with path="/mcp") resolves the remainder to "/mcp".
        # app_root_path remains on the scope for external URL generation.
        sub_scope = dict(scope)
        sub_scope["root_path"] = routing_root_path + "/" + name

        # Idle bookkeeping: an authenticated group request counts as in-flight
        # traffic for every member for the WHOLE delegation — a long-lived
        # Streamable-HTTP/SSE stream through the bundle must not have a member
        # bridge quiesced from under it; request_finished restarts each member's
        # idle clock when the stream closes. (Group requests don't WAKE idle
        # members — the bundle mounts running members only, and remounting happens
        # on the reconcile that follows a wake.) app.state.supervisor is assigned
        # in the lifespan before any request is served — fail fast if missing.
        #
        # The window OPENS BEFORE usage buffers the body: reading a slow upload can
        # take longer than a member's idle deadline, and a request already accepted
        # must not have its bridge stopped out from under it mid-upload.
        app = scope.get("app")
        supervisor = app.state.supervisor if app is not None else None
        for member_id in member_ids or []:
            if supervisor is not None:
                supervisor.request_started(member_id)
        try:
            # Count the tool calls this request carries (see _record_group_usage),
            # which may consume + replay the body. Only the bundle's own endpoint
            # counts — the inner app is mounted at "/mcp" alone, so a POST to any
            # other subpath 404s there and served nothing.
            #
            # Matched EXACTLY: "mcp/" and "mcp//" are 307s back to "/mcp", not
            # calls. Stripping slashes here counted the redirect AND the request
            # the client then followed it with, so one tool call landed twice.
            if subpath == "mcp":
                receive = await _record_group_usage(
                    request, receive, self._hub.mounted_members(name)
                )
            await inner(sub_scope, receive, send)
        finally:
            for member_id in member_ids or []:
                if supervisor is not None:
                    supervisor.request_finished(member_id)
