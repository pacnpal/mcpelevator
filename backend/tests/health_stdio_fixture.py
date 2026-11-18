"""A stdio MCP server for the health check tests (issue #225): ``tools/list`` always works,
while ``check`` fails (an isError result) whenever a ``health-fail`` file sits in the
working directory — so a test can make a running server start failing its check, and
recover, without restarting it. ``check`` with ``{"echo": ...}`` returns that text."""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

server = FastMCP("health-fixture")


@server.tool
def ping() -> str:
    return "pong"


@server.tool
def check(echo: str = "ok") -> str:
    marker = Path("health-fail")
    if marker.exists():
        raise ToolError(marker.read_text().strip() or "unhealthy")
    return echo


server.run()
