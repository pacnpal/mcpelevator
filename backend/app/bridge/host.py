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
sharing this process + the package install across the MCP and (later) REST
surfaces — never a single shared MCP session.

The REST/OpenAPI surface is added in M6; this M1 version serves MCP only.
"""

from __future__ import annotations

import json
import os

import uvicorn
from fastmcp import FastMCP
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
from app.runners.docker import (  # noqa: E402
    is_forbidden_container_env as _is_forbidden_container_env,
    is_reserved_docker_env as _is_reserved_docker_env,
)


def _child_env(spec: dict) -> dict[str, str]:
    """Environment for a stdio child.

    Default: merge the bridge's own environment (PATH, HOME, caches) with the
    server-specific vars so npx/uvx/etc. resolve; server vars win. When the spec sets
    ``minimal_env`` (the docker runner), pass ONLY the bridge's docker-CLI env (PATH/HOME +
    the operator's ``DOCKER_*`` config) plus the server's NON-reserved vars — never the full
    ``os.environ`` — so the elevator's own secrets can't leak into a container via a ``-e KEY``
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
    return {**os.environ, **server_env}


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

    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyHttpUrl

    async def _no_interactive(*_args, **_kwargs):
        raise RuntimeError(
            "this upstream needs OAuth sign-in — re-authenticate it from the mcpelevator UI"
        )

    url = oauth["url"]
    storage = ServerTokenStorage(oauth["server_id"])
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
        storage=storage,
        redirect_handler=_no_interactive,
        callback_handler=_no_interactive,
    )
    # Preload the discovered auth-server metadata so refresh knows the token endpoint
    # without a fresh 401/discovery round-trip (see ServerTokenStorage.set_metadata).
    metadata = storage.get_metadata()
    if metadata is not None:
        provider.context.oauth_metadata = metadata
    # Preload the stored token expiry so a lapsed access token is *refreshed* (silent)
    # rather than mistaken for valid and driven into the interactive 401 path.
    expiry = storage.get_token_expiry()
    if expiry is not None:
        provider.context.token_expiry_time = expiry
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
    if kind in ("streamable-http", "http"):
        return StreamableHttpTransport(
            url=spec["command"], headers=dict(spec.get("env") or {}), auth=auth
        )
    if kind == "sse":
        return SSETransport(url=spec["command"], headers=dict(spec.get("env") or {}), auth=auth)
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
    return create_proxy(client, name=spec.get("name") or "mcpelevator-proxy")


def main() -> None:
    """Entry point: read the ProcessSpec + port from the environment and serve."""
    spec = json.loads(os.environ["MCPE_BRIDGE_SPEC"])
    host = os.environ.get("MCPE_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ["MCPE_BRIDGE_PORT"])

    proxy = build_proxy(spec)
    # run() handles uvicorn + the Streamable HTTP session-manager lifespan for us.
    proxy.run(transport="http", host=host, port=port, show_banner=False)


if __name__ == "__main__":
    main()
