"""Per-server bridge host — the child process that does the actual elevation.

One bridge host == one FastMCP proxy in front of one stdio MCP server, served over
Streamable HTTP at ``/mcp`` by its own uvicorn on a loopback port. Running each
server in its own process gives fault isolation (a hung/crashing server can't take
down the control plane or its peers) and a real PID/port for supervision.

The control plane resolves the runner -> a literal ProcessSpec, then launches this
module as a subprocess, passing the spec + port via environment variables:

    MCPE_BRIDGE_SPEC  JSON: {command, args, env, cwd, name, transport, mcp_http, rest_openapi}
    MCPE_BRIDGE_HOST  loopback host (default 127.0.0.1)
    MCPE_BRIDGE_PORT  port to listen on

``transport`` selects the upstream: ``stdio`` (spawn command/args) or
``streamable-http`` / ``sse`` (front a remote URL; ``command`` is the URL and ``env``
is the upstream HTTP headers).

Session isolation: ``FastMCP.as_proxy(transport)`` gives a fresh upstream session
per request (no cross-client context mixing). "Sharing one subprocess" means
sharing this process + the package install across the MCP and REST surfaces —
never a single shared MCP session.

When the spec sets ``rest_openapi``, the same app additionally serves each tool as
plain REST (``POST /rest/<tool>``, body = the tool's JSON arguments) plus a
generated ``GET /rest/openapi.json`` — so non-MCP clients (curl, automation, GPT
Actions) can call the server's tools through the identical auth/proxy path. See
:func:`build_rest_routes`.
"""

from __future__ import annotations

import json
import os

from fastmcp import Client, FastMCP
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers.proxy import ProxyClient
from mcp.types import ClientCapabilities, Root, RootsCapability


async def _forward_roots(context) -> list[Root]:
    """Roots handler for the proxy's upstream (stdio) client.

    An upstream MCP server may ask its client — here, this proxy — to list the
    caller's filesystem roots. FastMCP's default proxy handler forwards that
    request straight to whichever client is connected to the elevator over HTTP.
    But many MCP clients (Claude.ai, and anything that connects without declaring
    the ``roots`` capability) reject ``roots/list``, and the upstream server then
    logs the rejection as a noisy, recurring::

        [FastMCP error] received error listing roots.
        McpError: MCP error -32603

    Forward the request only when the connected client actually advertises roots
    support, and degrade to an empty list on any failure. A client that can't
    list roots simply gets ``[]`` instead of a spurious error — which is exactly
    what "no roots" means to the upstream server.
    """
    try:
        ctx = get_context()
    except RuntimeError:
        # No active request context (e.g. the upstream asks during handshake).
        return []
    try:
        if not ctx.session.check_client_capability(
            ClientCapabilities(roots=RootsCapability())
        ):
            return []
        return await ctx.list_roots()
    except Exception:
        # Client advertised roots but failed to deliver them — don't surface the
        # failure to the upstream server as an internal error.
        return []


# SSOT for which env keys the docker CLI itself consumes lives in the docker runner (the
# service layer also rejects these as container env vars). ``is_reserved_docker_env`` is the
# NARROW set we inherit from the operator's env into the CLI; ``is_forbidden_container_env`` is
# the broader "a container must not supply this" set (adds Go proxy vars).
from app.config import is_control_plane_env_var as _is_control_plane_env_var  # noqa: E402
from app.runners.docker import (  # noqa: E402
    is_forbidden_container_env as _is_forbidden_container_env,
    is_reserved_docker_env as _is_reserved_docker_env,
)


def _child_env(spec: dict) -> dict[str, str]:
    """Environment for a stdio child.

    Default (npx/uvx/command): merge the bridge's inherited environment (PATH, HOME, proxy/CA,
    caches) with the server-specific vars so tools resolve; server vars win. The elevator's OWN
    ``MCPE_*`` config namespace is stripped first, so a passthrough server — even one that shells out
    to docker or reads ``BASH_ENV`` — does not INHERIT the control plane's secrets (admin token,
    signing keys, DB/data dir), and the secret subset is also kept out of the bridge parent's env
    (``ServerUnit._bridge_env``) so it isn't recoverable from ``/proc/<ppid>/environ``. NOTE: on a
    same-UID host without process isolation a child can still read an operator-supplied admin token
    from the control-plane process's own ``/proc/<pid>/environ``; a hard guarantee needs the docker
    runner, a separate UID / PID namespace / ``hidepid``, or supplying the token off-env. This env
    boundary is the primary in-process mitigation; the registry's shell-wrapped-docker detection is
    best-effort defense-in-depth on top of it. See docs/security.md.

    When the spec sets ``minimal_env`` (the docker runner), pass ONLY the bridge's docker-CLI env
    (PATH/HOME + the operator's ``DOCKER_*`` config) plus the server's NON-reserved vars — never the
    full ``os.environ`` — so the elevator's secrets can't leak into a container via a ``-e KEY``
    passthrough.
    """
    server_env = dict(spec.get("env") or {})
    if spec.get("minimal_env"):
        # The CLI's own env from the bridge: PATH/HOME + ALL the operator's DOCKER_* config
        # (DOCKER_HOST to reach dind, DOCKER_API_VERSION, DOCKER_CONFIG, …) so the runner CLI
        # behaves like the control plane's.
        base = {k: v for k, v in os.environ.items() if _is_reserved_docker_env(k)}
        # Strip forbidden keys from the server's env: a server-declared DOCKER_HOST /
        # DOCKER_API_VERSION / PATH must never retarget or alter the docker CLI (breaking dind
        # isolation or the daemon request), and a proxy var (HTTP_PROXY/…) must never land in the
        # CLI's env where it could reroute the control-plane's own daemon request on a TCP
        # DOCKER_HOST. `base` (the bridge's copies) wins. The service layer already rejects these;
        # this is defense in depth for a legacy row.
        safe = {k: v for k, v in server_env.items() if not _is_forbidden_container_env(k)}
        return {**safe, **base}
    # Scrub the control plane's own secrets from the inherited env before a passthrough child sees
    # it; a server may still set an ``MCPE_``-named var explicitly (it wins) — that carries the
    # operator's chosen value, not the elevator's.
    base = {k: v for k, v in os.environ.items() if not _is_control_plane_env_var(k)}
    return {**base, **server_env}


def _build_oauth_auth(oauth: dict):
    """Build a refresh-only OAuth httpx auth for a remote upstream.

    The interactive authorization already happened in the control plane (see
    ``app.auth.oauth_flow``); the tokens + DCR client info live in the shared file
    store. Here the bridge only needs an ``OAuthClientProvider`` that reads those
    tokens and refreshes them automatically as the access token expires. Discovery
    metadata is preloaded from the store so refresh targets the real token endpoint
    rather than the SDK's ``<server-url>/token`` fallback.

    The redirect/callback handlers raise: a bridge can't run an interactive sign-in.
    If the refresh token itself has lapsed, the upstream 401 leads here, the request
    fails, and the server goes unhealthy — the operator re-authenticates from the UI.
    """
    from app.auth.oauth_store import ServerTokenStorage  # local import: keeps bridge import light

    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyHttpUrl

    async def _no_interactive(*_args, **_kwargs):
        raise RuntimeError(
            "this upstream needs OAuth sign-in — re-authenticate it from the mcpelevator UI"
        )

    class _RefreshOnlyStorage(TokenStorage):
        """Bridge-side storage: reads tokens/client info and writes back REFRESHED tokens,
        but never persists a client REGISTRATION. Client registration is the control plane's
        interactive job. The primary guard against a probe-time DCR is the no-tokens early
        return in ``_build_oauth_auth`` (an unauthenticated server gets no provider at all);
        this no-op ``set_client_info`` is defense in depth for the residual case where tokens
        exist but a refresh has failed, so the SDK re-enters the 401 path — we still refuse to
        write a bridge-side registration (which would recreate a cleared file for the dummy
        loopback redirect)."""

        def __init__(self, inner: ServerTokenStorage):
            self._inner = inner

        async def get_tokens(self):
            return await self._inner.get_tokens()

        async def set_tokens(self, tokens) -> None:
            await self._inner.set_tokens(tokens)  # a real refresh must persist

        async def get_client_info(self):
            return await self._inner.get_client_info()

        async def set_client_info(self, client_info) -> None:
            return  # no-op: the bridge never registers a client

    url = oauth["url"]
    storage = ServerTokenStorage(oauth["server_id"])
    if not storage.status().get("authenticated"):
        # No stored access token yet (fresh create / post-Disconnect / lapsed refresh). Do
        # NOT attach an OAuth provider at all: with no stored client_info the SDK's 401 path
        # performs Dynamic Client Registration against the UPSTREAM on every readiness probe —
        # creating a throwaway client and burning the provider's registration quota — before
        # ``_no_interactive`` can stop it. (The no-op ``set_client_info`` below only blocks
        # LOCAL persistence of that registration, not the upstream call.) Returning ``None``
        # means the probe just gets a clean 401 with no OAuth requests at all, and the server
        # surfaces as needing sign-in. Re-authenticating from the UI restarts the bridge, which
        # rebuilds this with tokens present and a real refresh-capable provider.
        return None
    client_metadata = OAuthClientMetadata(
        client_name="mcpelevator",
        # Never used for refresh, but the model requires a redirect URI; a loopback
        # placeholder is fine (the bridge never performs an interactive grant).
        redirect_uris=[AnyHttpUrl("http://127.0.0.1/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=oauth.get("scopes") or None,
    )
    provider = OAuthClientProvider(
        server_url=url,
        client_metadata=client_metadata,
        storage=_RefreshOnlyStorage(storage),
        redirect_handler=_no_interactive,
        callback_handler=_no_interactive,
    )
    # Preload the discovered auth-server metadata so refresh knows the token endpoint
    # without a fresh 401/discovery round-trip (see ServerTokenStorage.set_metadata).
    metadata = storage.get_metadata()
    if metadata is not None:
        provider.context.oauth_metadata = metadata
    # Preload the protected-resource metadata too: if the auth server bound the tokens to
    # the PRM ``resource`` (a parent of the MCP URL), refresh must use that same resource,
    # not one recomputed from the URL — otherwise a resource-bound refresh is rejected.
    prm = storage.get_protected_resource_metadata()
    if prm is not None:
        provider.context.protected_resource_metadata = prm
    # Preload the stored token expiry so a lapsed access token is *refreshed* (silent)
    # rather than mistaken for valid and driven into the interactive 401 path.
    expiry = storage.get_token_expiry()
    if expiry is not None:
        provider.context.token_expiry_time = expiry
    # The SDK resets context.token_expiry_time from each refresh response's expires_in via
    # calculate_token_expiry(); when the provider OMITS expires_in that becomes None and
    # is_token_valid() then treats the access token as valid FOREVER — so a token that actually
    # lapses is sent stale and 401s into the (headless-impossible) interactive path until the
    # bridge restarts. Mirror the file store's default-TTL policy in memory (see
    # ServerTokenStorage._expires_at_for) so a running bridge keeps refreshing proactively:
    # when a refresh carries a refresh token but no expires_in, stamp the same modest fallback.
    import time as _time

    from mcp.shared.auth_utils import calculate_token_expiry

    from app.auth.oauth_store import _DEFAULT_REFRESHABLE_TTL

    _context = provider.context

    def _update_token_expiry(token) -> None:
        if token.expires_in is None and token.refresh_token:
            _context.token_expiry_time = _time.time() + _DEFAULT_REFRESHABLE_TTL
        else:
            _context.token_expiry_time = calculate_token_expiry(token.expires_in)

    # Instance-level override: the SDK calls ``self.context.update_token_expiry(token)`` from
    # both token-exchange and refresh handlers, so shadowing it on the instance covers both. If
    # a future SDK inlines the calc this simply stops applying (no breakage).
    _context.update_token_expiry = _update_token_expiry
    return provider


def _build_transport(spec: dict):
    """Pick the upstream transport from the spec's ``transport`` discriminator.

    ``stdio`` (the default) spawns the local command; ``streamable-http`` / ``sse``
    front an already-remote MCP URL. For the remote kinds ``command`` is the URL and
    ``env`` is the upstream HTTP headers — so they are NOT merged into ``os.environ``
    (that merge is only meaningful for a real child process). When ``oauth`` is set the
    upstream authenticates via an OAuth token (auto-refreshed) instead of static headers.
    """
    kind = spec.get("transport") or "stdio"
    oauth = spec.get("oauth")
    auth = _build_oauth_auth(oauth) if oauth else None
    headers = dict(spec.get("env") or {})
    if oauth:
        # OAuth owns the Authorization header. Drop any static one left over from a
        # Headers→OAuth switch: when there's no/expired OAuth token (startup, post-
        # disconnect, lapsed refresh) the auth object adds no bearer, and a stale static
        # token would otherwise still authenticate the upstream despite the UI saying
        # the server needs to re-authenticate.
        headers = {k: v for k, v in headers.items() if k.strip().lower() != "authorization"}
    if kind in ("streamable-http", "http"):
        return StreamableHttpTransport(url=spec["command"], headers=headers, auth=auth)
    if kind == "sse":
        return SSETransport(url=spec["command"], headers=headers, auth=auth)
    return StdioTransport(
        command=spec["command"],
        args=list(spec.get("args") or []),
        env=_child_env(spec),
        cwd=spec.get("cwd") or None,
    )


def build_proxy(spec: dict) -> FastMCP:
    """Build the FastMCP proxy that fronts one upstream MCP server.

    The upstream — a local stdio process or a remote HTTP/SSE URL, per
    :func:`_build_transport` — is wrapped in a ``ProxyClient`` carrying our tolerant
    roots handler (see :func:`_forward_roots`); all other advanced forwarding and the
    fresh-session-per-request isolation keep FastMCP's proxy defaults.
    """
    transport = _build_transport(spec)
    # Wrap the transport in a ProxyClient ourselves so we can install a roots
    # handler that tolerates clients without roots support (see _forward_roots).
    # Everything else — sampling, elicitation, logging, progress forwarding, and
    # the fresh-session-per-request isolation — keeps FastMCP's proxy defaults.
    client = ProxyClient(transport, roots=_forward_roots)
    proxy = create_proxy(client, name=spec.get("name") or "mcpelevator-proxy")
    # The outer proxy already authenticated the caller. Forwarding its headers from
    # this bridge to a remote MCP server would disclose bearer/OAuth credentials to
    # that upstream. Static headers and upstream OAuth live on the transport itself.
    if hasattr(transport, "forward_incoming_headers"):
        transport.forward_incoming_headers = False
    return proxy


# --- REST/OpenAPI surface (rest_openapi exposure) -----------------------------


def _rest_envelope(result) -> dict:
    """The stable REST response body for one tool call. MCP semantics are mirrored:
    ``is_error`` carries the tool's own failure (HTTP stays 200, like MCP's 200 +
    isError), ``structured_content`` is the tool's structured output when declared,
    and ``content`` is the raw MCP content blocks for everything else."""
    return {
        "is_error": bool(result.is_error),
        "content": [block.model_dump(mode="json") for block in result.content or []],
        "structured_content": result.structured_content,
    }


def _openapi_document(tools, name: str) -> dict:
    """An OpenAPI 3.1 document for the tool routes, generated from the live tool
    list so it always matches what the upstream currently serves. The relative
    ``servers`` url ("../") resolves against the document's own retrieval URL —
    ``…/s/<slug>/rest/openapi.json`` → ``…/s/<slug>`` — so the doc is correct
    behind the proxy without the bridge knowing its public slug or base URL."""
    paths: dict = {}
    for tool in tools:
        responses: dict = {
            "200": {
                "description": (
                    "Tool result envelope. `is_error` mirrors MCP's isError: the tool "
                    "itself failed but the call transported fine."
                ),
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "is_error": {"type": "boolean"},
                                "content": {"type": "array", "items": {"type": "object"}},
                                "structured_content": {
                                    **(tool.outputSchema or {}),
                                    "description": "Structured tool output (null unless the tool declares an outputSchema).",
                                },
                            },
                        }
                    }
                },
            }
        }
        paths[f"/rest/{tool.name}"] = {
            "post": {
                "operationId": tool.name,
                "summary": (tool.description or tool.name).strip().splitlines()[0][:120],
                "description": tool.description or "",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": tool.inputSchema or {"type": "object"}
                        }
                    },
                },
                "responses": responses,
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": name, "version": "1.0.0"},
        "servers": [{"url": "../"}],
        "paths": paths,
    }


def build_rest_routes(proxy: FastMCP, spec: dict) -> list:
    """Starlette routes for the REST surface, backed by in-memory client sessions
    against the same proxy the MCP surface serves — so each REST call gets the
    identical fresh-upstream-session semantics as an MCP request."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    name = spec.get("name") or "mcpelevator-proxy"

    async def openapi(request):
        async with Client(proxy) as client:
            tools = await client.list_tools()
        return JSONResponse(_openapi_document(tools, name))

    async def index(request):
        async with Client(proxy) as client:
            tools = await client.list_tools()
        return JSONResponse(
            {
                "tools": [
                    {"name": t.name, "description": t.description or "", "path": f"rest/{t.name}"}
                    for t in tools
                ],
                "openapi": "rest/openapi.json",
            }
        )

    async def call(request):
        tool = request.path_params["tool"]
        body = await request.body()
        if body:
            try:
                arguments = json.loads(body)
            except ValueError:
                return JSONResponse({"detail": "request body must be JSON"}, status_code=400)
            if not isinstance(arguments, dict):
                return JSONResponse(
                    {"detail": "request body must be a JSON object of tool arguments"},
                    status_code=400,
                )
        else:
            arguments = {}
        async with Client(proxy) as client:
            known = {t.name for t in await client.list_tools()}
            if tool not in known:
                return JSONResponse({"detail": f"unknown tool {tool!r}"}, status_code=404)
            result = await client.call_tool(tool, arguments, raise_on_error=False)
        return JSONResponse(_rest_envelope(result))

    return [
        Route("/rest", index, methods=["GET"]),
        Route("/rest/openapi.json", openapi, methods=["GET"]),
        Route("/rest/{tool}", call, methods=["POST"]),
    ]


def build_app(spec: dict, proxy: FastMCP):
    """The bridge's ASGI app: the Streamable-HTTP MCP surface at ``/mcp``, plus the
    REST routes when the server's ``rest_openapi`` exposure is on."""
    app = proxy.http_app(path="/mcp")
    if spec.get("rest_openapi"):
        # Static REST paths first; the {tool} route is POST-only so it can't shadow
        # the GET routes anyway, but explicit ordering keeps intent obvious.
        app.router.routes.extend(build_rest_routes(proxy, spec))
    return app


def main() -> None:
    """Entry point: read the ProcessSpec + port from the environment and serve."""
    spec = json.loads(os.environ["MCPE_BRIDGE_SPEC"])
    host = os.environ.get("MCPE_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ["MCPE_BRIDGE_PORT"])

    proxy = build_proxy(spec)
    if spec.get("rest_openapi"):
        # Compose MCP + REST into one app; run uvicorn directly (http_app carries
        # the session-manager lifespan, which uvicorn executes).
        import uvicorn

        uvicorn.run(build_app(spec, proxy), host=host, port=port, log_level="info")
    else:
        # run() handles uvicorn + the Streamable HTTP session-manager lifespan for us.
        proxy.run(transport="http", host=host, port=port, show_banner=False)


if __name__ == "__main__":
    main()
