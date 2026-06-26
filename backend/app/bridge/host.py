"""Per-server bridge host — the child process that does the actual elevation.

One bridge host == one FastMCP proxy in front of one stdio MCP server, served over
Streamable HTTP at ``/mcp`` by its own uvicorn on a loopback port. Running each
server in its own process gives fault isolation (a hung/crashing server can't take
down the control plane or its peers) and a real PID/port for supervision.

The control plane resolves the runner -> a literal ProcessSpec, then launches this
module as a subprocess, passing the spec + port via environment variables:

    MCPE_BRIDGE_SPEC  JSON: {command, args, env, cwd, name, mcp_http, rest_openapi}
    MCPE_BRIDGE_HOST  loopback host (default 127.0.0.1)
    MCPE_BRIDGE_PORT  port to listen on

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
from fastmcp.client.transports import StdioTransport


def build_proxy(spec: dict) -> FastMCP:
    transport = StdioTransport(
        command=spec["command"],
        args=list(spec.get("args") or []),
        # Merge the child's own environment (PATH, HOME, caches) with the
        # server-specific vars so npx/uvx/etc. resolve; server vars win.
        env={**os.environ, **(spec.get("env") or {})},
        cwd=spec.get("cwd") or None,
    )
    return FastMCP.as_proxy(transport, name=spec.get("name") or "mcpelevator-proxy")


def main() -> None:
    spec = json.loads(os.environ["MCPE_BRIDGE_SPEC"])
    host = os.environ.get("MCPE_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ["MCPE_BRIDGE_PORT"])

    proxy = build_proxy(spec)
    # run() handles uvicorn + the Streamable HTTP session-manager lifespan for us.
    proxy.run(transport="http", host=host, port=port, show_banner=False)


if __name__ == "__main__":
    main()
