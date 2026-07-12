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


# Environment keys a docker CLI child genuinely needs from the control plane's env when
# ``minimal_env`` is set. Everything else (MCPE_ADMIN_TOKEN, DB creds, unrelated API keys)
# is deliberately withheld so a container's ``-e KEY`` passthrough can't reach it.
_DOCKER_ENV_ALLOWLIST = (
    "PATH", "HOME",
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONTEXT", "DOCKER_CONFIG",
)


def _child_env(spec: dict) -> dict[str, str]:
    """Environment for a stdio child.

    Default: merge the bridge's own environment (PATH, HOME, caches) with the
    server-specific vars so npx/uvx/etc. resolve; server vars win. When the spec sets
    ``minimal_env`` (the docker runner), pass ONLY a small allowlist of the bridge's env
    plus the server vars — never the full ``os.environ`` — so the elevator's own secrets
    can't leak into a container via a ``-e KEY`` passthrough.
    """
    server_env = dict(spec.get("env") or {})
    if spec.get("minimal_env"):
        base = {k: os.environ[k] for k in _DOCKER_ENV_ALLOWLIST if k in os.environ}
        # base (the bridge's PATH/HOME + DOCKER_* connection vars) WINS over server_env: a
        # server-declared DOCKER_HOST must not retarget the docker CLI (which would break
        # the dind-sidecar isolation), nor a PATH override stop `docker` from resolving.
        return {**server_env, **base}
    return {**os.environ, **server_env}


def _build_transport(spec: dict):
    """Pick the upstream transport from the spec's ``transport`` discriminator.

    ``stdio`` (the default) spawns the local command; ``streamable-http`` / ``sse``
    front an already-remote MCP URL. For the remote kinds ``command`` is the URL and
    ``env`` is the upstream HTTP headers — so they are NOT merged into ``os.environ``
    (that merge is only meaningful for a real child process).
    """
    kind = spec.get("transport") or "stdio"
    if kind in ("streamable-http", "http"):
        return StreamableHttpTransport(url=spec["command"], headers=dict(spec.get("env") or {}))
    if kind == "sse":
        return SSETransport(url=spec["command"], headers=dict(spec.get("env") or {}))
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
