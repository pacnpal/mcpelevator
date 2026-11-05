"""Per-server cache directories — where a local server's downloads survive a recreate.

npm and uv already cache on the data volume (``npm_config_cache`` / ``UV_CACHE_DIR``, set
image-wide and shared by every server). Everything else a server caches follows
``XDG_CACHE_HOME`` (Hugging Face models, fastembed/onnx, …) or ``PLAYWRIGHT_BROWSERS_PATH``
(Playwright browsers), which default under ``HOME`` — outside the volume. Each local-runner
server (npx/uvx/command) gets its own ``<data_dir>/cache/<server id>`` instead, pointed to
by both variables in its setup/checkout shells and its launched child; the server's own
``env`` still wins key by key. Its own directory, rather than one shared one, is what lets
an operator clear a single server's cache (a download cached corrupt, say) without touching
any other server's.

Not part of ``config_hash``: the directory is derived from the id, so it is constant for a
server's lifetime and adding it changed no existing hash. Docker-runner children have their
own filesystem and a minimal env; remote/openapi/built-in servers launch no child.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CACHE_DIRNAME = "cache"
# The runners whose child is a local process inheriting the bridge's environment.
RUNNERS = ("npx", "uvx", "command")
# Hands the directory from the unit to the bridge, which sets the child's env from it
# (``bridge.host._child_env``). Bridge-process env, not a spec field: the spec travels in
# one size-capped env var and a new always-present key would grow every persisted one.
# ``MCPE_``-prefixed, so the child never inherits it.
BRIDGE_ENV_KEY = "MCPE_BRIDGE_CACHE_DIR"


def has_cache(runner: str) -> bool:
    return runner in RUNNERS


def cache_root(data_dir: Path) -> Path:
    return data_dir.resolve() / CACHE_DIRNAME


def cache_dir(data_dir: Path, server_id: str) -> Path:
    """``<data_dir>/cache/<server_id>``; ``ValueError`` unless the id is one plain path
    component (it is a generated hex id, but it lands in a path that gets deleted)."""
    if (
        not server_id
        or server_id in (".", "..")
        or "/" in server_id
        or "\\" in server_id
        or "\0" in server_id
    ):
        raise ValueError(f"not a server id: {server_id!r}")
    return cache_root(data_dir) / server_id


def cache_env(directory: str | os.PathLike) -> dict[str, str]:
    """The child env that points a server's caches at ``directory``."""
    directory = str(directory)
    return {
        "XDG_CACHE_HOME": directory,
        "PLAYWRIGHT_BROWSERS_PATH": os.path.join(directory, "ms-playwright"),
    }


def _size(path: Path) -> int:
    """Bytes under ``path``, never following a symlink (a server can plant one)."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def clear_cache(data_dir: Path, server_id: str) -> int:
    """Delete a server's cache directory; returns the bytes it held (0 when absent).

    Only ever the one directory directly under ``<data_dir>/cache``: the path is built from
    the resolved root and checked to sit right under it, and a symlink in its place (a
    server's child can write there) is unlinked, never followed. Raises ``OSError`` when the
    directory can't be removed."""
    root = cache_root(data_dir)
    target = cache_dir(data_dir, server_id)
    if target.parent != root:
        raise ValueError(f"cache path escapes {root}: {target}")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        target.unlink()
        return 0
    if not target.exists():
        return 0
    freed = _size(target)
    shutil.rmtree(target)
    return freed


def remove_cache(data_dir: Path, server_id: str) -> None:
    """:func:`clear_cache` for a server delete: best-effort, since a leftover directory
    is only wasted disk, never a reason to fail the delete."""
    try:
        clear_cache(data_dir, server_id)
    except (OSError, ValueError):
        pass
