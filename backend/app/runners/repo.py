"""Source checkouts — a local-runner server launched from a git repository.

Plenty of MCP servers are never published to npm or PyPI: their README says "clone,
build, run" (e.g. ``github.com/ooples/mcp-console-automation``, whose ``npx -y github:…``
form fails). A server with a ``repo_url`` gets that flow managed for it:

1. **checkout** — ``<data_dir>/repos/<server id>`` is cloned on first start and re-fetched
   at ``repo_ref`` (a branch, tag or commit; blank = the default branch) on every
   activation. If the fetch fails but an earlier checkout exists, that one is started, so a
   GitHub outage doesn't take a built server down.
2. **post-clone** — the operator's ``post_clone_script`` (``npm ci && npm run build``, …)
   runs in the checkout, but only when the checked-out commit or the script itself changed
   since the last successful run (a stamp file beside the checkout records both), so a
   restart doesn't rebuild.
3. the ordinary setup script (if any) and the launch command then run with the checkout
   as their working directory (a relative ``cwd`` is taken inside it).

Both steps run as one generated POSIX script through the unit's setup machinery (same
scrubbed env, log streaming and process-group teardown), under ``repo_build_timeout_s``
rather than the shorter ``start_timeout_s``: a first build can take minutes.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

REPOS_DIRNAME = "repos"

# Branch / tag / commit. Narrower than git's ref grammar: it lands in a git argv and a
# shell script, so nothing that could read as an option or as syntax.
_REF = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")
# scp-like clone address: git@github.com:owner/repo.git
_SCP = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+$")
# owner/repo or github:owner/repo — GitHub shorthand, expanded to an https URL.
_SHORTHAND = re.compile(r"^(?:github:)?([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})$")
# Network transports only: file:// would read the box's own disk and ext:: runs a command.
_SCHEMES = {"https", "http", "ssh", "git"}
# Environment variables pointing a Python tool at mcpelevator's OWN virtualenv
# (`uv run uvicorn …` exports VIRTUAL_ENV; uv sync/run also honor UV_PROJECT_ENVIRONMENT).
# A checkout builds into, and launches from, its own .venv, so neither the build nor the
# launched server inherits them (a server's explicit env still sets them at launch).
FOREIGN_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")


def normalize_repo(url: Optional[str], ref: Optional[str]) -> tuple[str, str]:
    """Canonicalize ``(repo_url, repo_ref)``; ``ValueError`` with an operator-facing reason.

    Blank url = no repository (the ref is cleared too). GitHub shorthand and a pasted
    ``https://github.com/o/r/tree/<ref>`` page URL are both accepted; the page URL's ref
    fills ``ref`` unless one was given explicitly.
    """
    url = (url or "").strip()
    ref = (ref or "").strip()
    if not url:
        return "", ""
    if any(c.isspace() or ord(c) < 32 for c in url) or url.startswith("-"):
        raise ValueError("repository URL must be a single git URL")
    short = _SHORTHAND.match(url)
    if short and "://" not in url:
        url = f"https://github.com/{short.group(1)}/{short.group(2).removesuffix('.git')}"
    elif _SCP.match(url) and "://" not in url:
        pass
    else:
        if url.startswith("git+"):
            url = url[len("git+"):]
        parts = urlsplit(url)
        if parts.scheme.lower() not in _SCHEMES or not parts.hostname:
            raise ValueError(
                "repository URL must be an https://, ssh://, git:// or git@host:path address"
            )
        host = parts.hostname.lower()
        if host in ("github.com", "www.github.com"):
            # A browser URL (…/tree/<ref>, #readme, ?tab=…) → the clone URL, keeping any
            # credentials an operator put in it for a private repository.
            segs = [s for s in parts.path.split("/") if s]
            if len(segs) < 2:
                raise ValueError("expected https://github.com/<owner>/<repo>")
            if len(segs) >= 4 and segs[2] in ("tree", "commit") and not ref:
                ref = "/".join(segs[3:])
            netloc = re.sub(r"(?i)www\.github\.com", "github.com", parts.netloc)
            url = urlunsplit(
                (parts.scheme, netloc, f"/{segs[0]}/{segs[1].removesuffix('.git')}", "", "")
            )
    if ref and (not _REF.match(ref) or ".." in ref or ref.endswith("/")):
        raise ValueError(f"unsupported branch, tag or commit name: {ref!r}")
    return url, ref


def display_url(url: str) -> str:
    """The repository URL safe to log: any ``user:token@`` credentials removed."""
    if "://" not in url:
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[1], parts.path, "", ""))


def checkout_dir(data_dir: Path, server_id: str) -> Path:
    return data_dir.resolve() / REPOS_DIRNAME / server_id


def stamp_path(data_dir: Path, server_id: str) -> Path:
    # Beside the checkout, not in it: nothing a build or `git clean` does can touch it.
    return data_dir.resolve() / REPOS_DIRNAME / f"{server_id}.built"


def checkout_script(
    url: str, ref: str, post_clone_script: str, directory: Path, stamp: Path
) -> str:
    """The POSIX script (run as ``sh -e -c``) that brings ``directory`` to ``ref`` of ``url``
    and runs ``post_clone_script`` there when the commit or the script changed. Pure."""
    q = shlex.quote
    script_hash = hashlib.sha256(post_clone_script.encode()).hexdigest()[:16]
    post = (
        f"/bin/sh -e -c {q(post_clone_script)}\n" if post_clone_script.strip() else ""
    )
    return f"""\
dir={q(str(directory))}
url={q(url)}
ref={q(ref or "HEAD")}
stamp={q(str(stamp))}
export GIT_TERMINAL_PROMPT=0
if [ ! -d "$dir/.git" ] || [ "$(git -C "$dir" config --get remote.origin.url || true)" != "$url" ]; then
  echo {q(f"[mcpelevator] cloning {display_url(url)}")}
  rm -rf "$dir" "$stamp"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" remote add origin "$url"
fi
if git -C "$dir" fetch -q --depth 1 origin "$ref"; then
  sha=$(git -C "$dir" rev-parse FETCH_HEAD)
elif sha=$(git -C "$dir" rev-parse -q --verify HEAD); then
  echo "[mcpelevator] fetch failed; starting the existing checkout $sha"
else
  echo "[mcpelevator] fetch of $ref failed and there is no earlier checkout" >&2
  exit 1
fi
if [ "$(git -C "$dir" rev-parse -q --verify HEAD || true)" != "$sha" ]; then
  git -C "$dir" checkout -q --force --detach "$sha"
fi
want="$sha {script_hash}"
if [ "$(cat "$stamp" 2>/dev/null || true)" = "$want" ]; then
  echo "[mcpelevator] checkout $sha is already built"
  exit 0
fi
rm -f "$stamp"
echo "[mcpelevator] checked out $sha; running post-clone script"
cd "$dir"
# The build is for this checkout, never for whatever virtualenv mcpelevator itself runs in.
unset {" ".join(FOREIGN_ENV)}
{post}printf '%s\\n' "$want" > "$stamp"
"""


def remove_checkout(data_dir: Path, server_id: str) -> None:
    """Delete a server's checkout and build stamp (on server delete). Best-effort: a
    leftover directory is only wasted disk, never a reason to fail the delete."""
    shutil.rmtree(checkout_dir(data_dir, server_id), ignore_errors=True)
    try:
        stamp_path(data_dir, server_id).unlink(missing_ok=True)
    except OSError:
        pass
