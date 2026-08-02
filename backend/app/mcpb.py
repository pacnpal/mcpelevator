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

from app import __version__
from app.db.models import Server
from app.runners import build_spec


def manifest(server: Server) -> dict:
    """The MCPB ``manifest.json`` for a local stdio server.

    Raises ``ValueError`` for a non-stdio (remote) server, and for a spec that
    depends on launch context a bundle can't carry (``cwd``/``setup_script``) —
    exporting those would hand out a bundle that can't reproduce the server.
    ``env`` is embedded verbatim — the download is control-plane-gated, and the
    same principal already reads those values on the server detail endpoint.
    ``disabled_tools`` is deliberately NOT enforced: it filters the elevator's
    exposed surfaces, and the downloader is the operator who set that policy —
    a local run is their own machine, outside the elevator's enforcement.
    """
    spec = build_spec(server)
    if spec.transport != "stdio":
        raise ValueError("only local stdio servers can be exported as .mcpb")
    if spec.cwd or spec.setup_script:
        raise ValueError(
            "this server depends on a working directory or setup script, "
            "which a .mcpb bundle cannot carry"
        )
    # Version = elevator release (release-tag-derived, never hardcoded — see
    # app.__init__) + the row's config_hash as semver build metadata (hex + dots,
    # which is valid there), so a same-release config edit still yields a
    # distinguishable version string. Split off any existing build metadata
    # (the "0.0.0+unknown" fallback) — semver allows only one "+".
    version = __version__.lstrip("v").split("+", 1)[0]
    if server.config_hash:
        version = f"{version}+{server.config_hash}"
    mcp_config: dict = {"command": spec.command, "args": list(spec.args)}
    if spec.env:
        mcp_config["env"] = dict(spec.env)
    return {
        # 0.2 is the MCPB baseline every bundle-aware client accepts; nothing
        # here needs a newer manifest feature.
        "manifest_version": "0.2",
        # Package identity = the immutable server id: a slug rename or a
        # same-slug server on another instance must not fork/collide the
        # installed extension. The human-facing name lives in display_name
        # (and the download filename stays <slug>.mcpb — cosmetic only).
        "name": server.id,
        "display_name": server.name,
        "version": version,
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
