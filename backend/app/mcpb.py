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
import re
import zipfile

from app import __version__
from app.db.models import Server
from app.runners import build_spec
from app.runners.docker import server_label


# One SemVer prerelease identifier (semver.org grammar): a numeric one can't have
# leading zeroes, an alphanumeric one needs a letter or hyphen.
_PRERELEASE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
# node-semver refuses a numeric component above JavaScript's Number.MAX_SAFE_INTEGER.
_MAX_SAFE_INTEGER = 2**53 - 1


def _semver(raw: str) -> str:
    """``raw`` (a release tag / pyproject version) as strict SemVer.

    Claude Desktop parses the manifest version with node ``semver`` and a string it
    rejects is fatal: the extension installs, then the app crashes on every launch
    until the bundle is deleted by hand (anthropics/mcpb#226). So the core is always
    three plain integers within node-semver's bounds — a leading ``v``, a PEP 440
    suffix (``1.7.0rc1``, ``1.7.0.dev0``), ``0.0.0+unknown`` or a four-part date all
    fall back to a valid core (``int()`` also strips a leading zero, which semver
    forbids), and an oversized component falls back to ``0.0.0``. A prerelease that
    is already valid SemVer (``1.7.0-rc.1`` — the release workflow admits these)
    is kept: it sorts BELOW the stable release, so a consumer comparing versions
    sees the later stable bundle as an upgrade rather than the same version.
    """
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        return "0.0.0"
    parts = [int(g) for g in m.groups()]
    if max(parts) > _MAX_SAFE_INTEGER:
        return "0.0.0"
    core = ".".join(map(str, parts))
    pre = re.match(rf"-({_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*)(?:\+|$)", raw[m.end():])
    return f"{core}-{pre.group(1)}" if pre else core


def manifest(server: Server) -> dict:
    """The MCPB ``manifest.json`` for a local stdio server.

    Raises ``ValueError`` for a non-stdio (remote) server, and for a spec that
    depends on launch context a bundle can't carry (``cwd``/``setup_script``) —
    exporting those would hand out a bundle that can't reproduce the server.
    ``env`` is embedded verbatim — the download is control-plane-gated, and the
    same principal already reads those values on the server detail endpoint.
    The per-tool policy (``disabled_tools``, ``tool_overrides``) is deliberately
    NOT carried: it shapes the elevator's exposed surfaces, and the downloader is
    the operator who set that policy — a local run is their own machine, talking
    to the upstream server directly, outside the elevator's enforcement.
    """
    spec = build_spec(server)
    if spec.transport != "stdio":
        raise ValueError("only local stdio servers can be exported as .mcpb")
    if spec.cwd or spec.setup_script:
        raise ValueError(
            "this server depends on a working directory or setup script, "
            "which a .mcpb bundle cannot carry"
        )
    # A relative command path (./server, bin\server.exe) resolves against the
    # elevator's own working directory — it cannot exist where a client launches
    # the bundle. Bare names (npx, python) PATH-resolve; absolute paths
    # (/usr/bin/x, C:\tools\x.exe, \\host\share\x) are well-defined — both stay
    # exportable. Args are not analyzed: whether "server.py" is a file reference
    # or an opaque token is undecidable here.
    cmd = spec.command
    is_path = "/" in cmd or "\\" in cmd
    is_absolute = cmd.startswith(("/", "\\\\")) or bool(re.match(r"[A-Za-z]:[\\/]", cmd))
    if is_path and not is_absolute:
        raise ValueError(
            "this server's command is a relative path, "
            "which won't resolve outside the elevator"
        )
    # Version = elevator release (release-tag-derived, never hardcoded — see
    # app.__init__), coerced to strict semver, + the row's config_hash as build
    # metadata (``hex.hex`` — valid there) so a same-release config edit still
    # yields a distinguishable version string. The metadata is guarded by semver's
    # own identifier grammar: an invalid tail would be as fatal as an invalid core.
    version = _semver(__version__)
    if server.config_hash and re.fullmatch(r"[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*", server.config_hash):
        version = f"{version}+{server.config_hash}"
    args = list(spec.args)
    if server.runner == "docker":
        # The docker runner labels its containers so the elevator can ``docker rm -f``
        # them (boot orphan sweep, unit stop). A bundle run against the SAME daemon —
        # Claude Desktop beside a locally hosted elevator — must not carry that mark,
        # or the elevator would reap the user's own container. The builder emits
        # exactly one adjacent ``--label <selector>`` pair (before any run_args, and
        # the service refuses operator-set reserved labels), so drop just that pair.
        for i in range(len(args) - 1):
            if args[i] == "--label" and args[i + 1] == server_label(server.id):
                del args[i : i + 2]
                break
    mcp_config: dict = {"command": spec.command, "args": args}
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
            # Claude Desktop routes a binary bundle to plain exec: it resolves a
            # bare command against the user's login-shell PATH (probing
            # .exe/.bat/.cmd/.ps1 on Windows and wrapping a .cmd shim in
            # ``cmd.exe /C`` itself) and merges ``env`` into the child's
            # environment — so no ``platform_overrides``/``npx.cmd`` shims,
            # ``user_config`` indirection, or newer manifest_version is needed.
            "type": "binary",
            "entry_point": spec.command,
            "mcp_config": mcp_config,
        },
    }


def exportable(server: Server) -> bool:
    """Would :func:`manifest` accept this server? The single eligibility source
    for the UI (via the detail response) — no mirrored client-side rule to drift."""
    try:
        manifest(server)
        return True
    except ValueError:
        return False


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
