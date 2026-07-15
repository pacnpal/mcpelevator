from __future__ import annotations

import os
from pathlib import Path

from fastmcp import FastMCP


expected_cwd = os.environ.get("EXPECTED_CWD")
if expected_cwd and Path.cwd() != Path(expected_cwd):
    raise RuntimeError(f"unexpected cwd: {Path.cwd()}")
if os.environ.get("SETUP_ONLY"):
    raise RuntimeError("setup shell environment leaked into the MCP child")
if not Path("setup-complete").exists():
    raise RuntimeError("MCP child launched before setup completed")

server = FastMCP("stdio-fixture")


@server.tool
def ping() -> str:
    return "pong"


server.run()
