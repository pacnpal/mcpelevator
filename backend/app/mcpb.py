"""MCPB export — package a local stdio server as a downloadable ``.mcpb`` bundle.

An MCPB bundle (https://github.com/anthropics/mcpb) is a zip whose
``manifest.json`` tells an MCP client (e.g. Claude Desktop) how to launch the
server locally. We generate it on the fly from the ``Server`` row through the
same runner builder the bridge uses (``runners.build_spec``), so the bundle
always launches exactly what the elevator runs — no stored artifact to drift.

Only stdio specs qualify: a ``remote`` server has nothing to run locally.
"""

from __future__ import annotations

import io
import json
import zipfile

from app.db.models import Server
from app.runners import build_spec


def manifest(server: Server) -> dict:
    """The MCPB ``manifest.json`` for a local stdio server.

    Raises ``ValueError`` for a non-stdio (remote) server. ``env`` is embedded
    verbatim — the download is control-plane-gated, and the same principal
    already reads those values on the server detail endpoint. ``cwd`` and
    ``setup_script`` have no MCPB equivalent and are not represented.
    """
    spec = build_spec(server)
    if spec.transport != "stdio":
        raise ValueError("only local stdio servers can be exported as .mcpb")
    mcp_config: dict = {"command": spec.command, "args": list(spec.args)}
    if spec.env:
        mcp_config["env"] = dict(spec.env)
    return {
        # 0.2 is the MCPB baseline every bundle-aware client accepts; nothing
        # here needs a newer manifest feature.
        "manifest_version": "0.2",
        "name": server.slug,
        "display_name": server.name,
        "version": "1.0.0",
        "description": f"{server.name} ({server.runner}: {server.command}) — exported from mcpelevator",
        "author": {"name": "mcpelevator"},
        "server": {
            # "binary": the bundle ships no code — mcp_config invokes the
            # host's own npx/uvx/docker/executable, same argv as the bridge.
            "type": "binary",
            "entry_point": spec.command,
            "mcp_config": mcp_config,
        },
    }


def bundle(server: Server) -> bytes:
    """The ``.mcpb`` file bytes: a zip containing only ``manifest.json``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Fixed ZipInfo timestamp (the 1980 zip epoch): same row, byte-identical bundle.
        zf.writestr(
            zipfile.ZipInfo("manifest.json"),
            json.dumps(manifest(server), indent=2) + "\n",
        )
    return buf.getvalue()
