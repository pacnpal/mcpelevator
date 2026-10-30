"""Install from a source repository — a GitHub repo URL → a repository-checkout draft.

Plenty of MCP servers are never published to npm or PyPI: the README says "clone, build,
run" (e.g. ``github.com/ooples/mcp-console-automation``, where ``npx -y github:…`` fails).
Those run as a ``command`` server with a repository checkout (``app.runners.repo``):
mcpelevator clones the repo, runs a post-clone script, and launches from the checkout.
This module drafts that config from the repo's root manifests, detecting what it can:

    package.json   → post-clone: <npm ci | npm install | pnpm … | yarn …> [&& <pm> run build]
                     command:    node <bin> | npm run --silent start | node <main>
    pyproject.toml → post-clone: uv sync [--frozen]
                     command:    uv run --no-sync <script>

Detection is best effort: whatever it can't work out is left blank with a reason, and every
field stays editable in the review form (and later, on the server's edit page).
``parse_repo_url`` and ``repo_detail`` are pure; ``get_repository`` is the only I/O — raw-file
requests whose URLs are built from validated parts, never from the operator's string, so
this can't be pointed at an arbitrary host. The result is reviewed in the normal form and
posted to ``POST /api/servers`` like every catalog draft.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tomllib
from typing import Any, NamedTuple
from urllib.parse import quote, urlsplit

import httpx

from app.catalog import mapping
from app.catalog.base import CatalogUpstreamError

logger = logging.getLogger(__name__)

SOURCE_ID = "github"
SOURCE_LABEL = "GitHub"

RAW_BASE = "https://raw.githubusercontent.com"
_TIMEOUT = httpx.Timeout(15.0)
# A manifest is a few KB; anything far past that isn't one worth parsing.
_MAX_MANIFEST_BYTES = 512 * 1024

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Branch / tag / commit. Deliberately narrower than git's ref grammar: it lands in a git argv,
# a shell script and a raw-file path, so nothing that could be read as syntax by any of them.
_REF = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")
_HOSTS = {"github.com", "www.github.com"}

_FIRST_START_NOTE = (
    "mcpelevator clones the repository and runs the post-clone script on first start "
    "(which can take a few minutes), then again only when the commit or the script "
    "changes. Edit the detected steps below if they're not right."
)
# Lockfiles that pick the install command, in precedence order.
LOCKFILES = ("pnpm-lock.yaml", "yarn.lock", "package-lock.json", "npm-shrinkwrap.json", "uv.lock")
_JS_EXT = (".js", ".mjs", ".cjs")


class RepoRef(NamedTuple):
    owner: str
    repo: str
    ref: str | None = None  # None = the repository's default branch
    # The clone URL to store when the pasted one carries something the canonical
    # https://github.com/<owner>/<repo> would lose: an SSH transport or https credentials
    # (a private repository). None = clone over anonymous https.
    clone_url: str | None = None


def _valid(owner: str, repo: str, ref: str | None) -> RepoRef:
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER.match(owner) or not _REPO.match(repo) or repo in (".", ".."):
        raise ValueError(f"not a GitHub repository: {owner}/{repo}")
    if ref is not None:
        if not _REF.match(ref) or ".." in ref or ref.endswith("/"):
            raise ValueError(f"unsupported branch, tag or commit name: {ref!r}")
    return RepoRef(owner, repo, ref or None)


def parse_repo_url(url: str) -> RepoRef:
    """Parse what an operator would paste for a GitHub repository.

    Accepts ``https://github.com/<owner>/<repo>`` (``.git``, a trailing slash, a
    ``/tree/<ref>`` / ``/commit/<sha>`` / ``/releases/tag/<tag>`` suffix, a query or a
    ``#readme``-style fragment are all fine), ``git+https://``/``git://``/``ssh://`` and
    ``git@github.com:<owner>/<repo>.git`` clone URLs, the npm shorthand
    ``github:<owner>/<repo>[#ref]``, and a bare ``<owner>/<repo>``. Raises ``ValueError``
    with an operator-facing reason for anything else — including a subdirectory of a
    repository, which neither npm nor this resolver can install from.
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("enter a GitHub repository URL")

    # npm's shorthand is the one form where a fragment names the ref.
    if text.startswith("github:"):
        body, _, ref = text[len("github:"):].partition("#")
        parts = body.strip("/").split("/")
        if len(parts) != 2:
            raise ValueError("expected github:<owner>/<repo>")
        return _valid(parts[0], parts[1], ref or None)

    # How to clone it, when that isn't anonymous https: (form, userinfo) — rebuilt from
    # the parsed owner/repo below, so a /tree/<ref> suffix or fragment never leaks in.
    transport: tuple[str, str] | None = None
    # scp-like clone URL: git@github.com:owner/repo.git
    scp = re.match(r"^(?P<user>[\w.-]+)@(?P<host>[^:/]+):(?P<path>.+)$", text)
    if scp and "://" not in text:
        host, path = scp.group("host").lower(), scp.group("path")
        transport = ("scp", scp.group("user"))
    elif "://" in text or text.lower().startswith(("github.com/", "www.github.com/")):
        parsed = urlsplit(text if "://" in text else f"https://{text}")
        host, path = (parsed.hostname or "").lower(), parsed.path
        userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
        scheme = parsed.scheme.lower().removeprefix("git+")
        if scheme == "ssh":
            transport = ("ssh", userinfo or "git")
        elif scheme == "https" and userinfo:
            transport = ("https", userinfo)
    else:
        host, path = "github.com", text  # bare owner/repo
    if host not in _HOSTS:
        raise ValueError("only GitHub repositories (github.com) are supported")

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("expected a repository URL like https://github.com/<owner>/<repo>")
    owner, repo, rest = parts[0], parts[1], parts[2:]
    ref: str | None = None
    if rest:
        if rest[0] in ("tree", "commit") and len(rest) >= 2:
            ref, extra = rest[1], rest[2:]
        elif rest[:2] == ["releases", "tag"] and len(rest) >= 3:
            ref, extra = rest[2], rest[3:]
        else:
            extra = rest
        if extra:
            # /tree/main/packages/foo, /blob/…: a path inside the repository. An ambiguous
            # slash-containing branch also lands here; github:owner/repo#feature/x names it.
            raise ValueError(
                "point at the repository root — installing from a subdirectory isn't "
                "supported (for a branch name containing '/', use github:<owner>/<repo>#<branch>)"
            )
    found = _valid(owner, repo, ref)
    if transport is None:
        return found
    form, userinfo = transport
    where = f"{found.owner}/{found.repo}"
    clone_url = {
        "scp": f"{userinfo}@github.com:{where}.git",
        "ssh": f"ssh://{userinfo}@github.com/{where}.git",
        "https": f"https://{userinfo}@github.com/{where}",
    }[form]
    return found._replace(clone_url=clone_url)


def _unscoped(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _bins(pkg: dict[str, Any]) -> dict[str, str]:
    """The package's executables, as npm itself reads ``bin`` (a string = one bin named
    after the unscoped package)."""
    raw = pkg.get("bin")
    name = pkg.get("name")
    if isinstance(raw, str) and raw.strip() and isinstance(name, str) and name:
        return {_unscoped(name): raw}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and k and v}
    return {}


def _rel(path: str) -> str:
    """A manifest path (``./dist/x.js``) as an argv token relative to the checkout."""
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _repo_draft(index: int, kind: str, ref: RepoRef) -> dict[str, Any]:
    """The common shape: a ``command`` server launched from a checkout of ``ref``."""
    draft = mapping.blank_draft(index, kind, f"{ref.owner}/{ref.repo}", ref.ref)
    draft["runner"] = "command"
    draft["repo_url"] = ref.clone_url or f"https://github.com/{ref.owner}/{ref.repo}"
    draft["repo_ref"] = ref.ref or ""
    draft["post_clone_script"] = ""
    return draft


# Corepack runs the exact pnpm/yarn a repo pins (`packageManager`). It no longer ships with
# Node (25+, and the image runs 26), so it's fetched through npx and invoked as
# `corepack <pm> …` rather than `corepack enable` — no global shims to write. The prompt
# would otherwise ask before downloading the package manager.
_COREPACK = "COREPACK_ENABLE_DOWNLOAD_PROMPT=0 npx -y corepack"


def _node_install(pkg: dict[str, Any], lockfiles: set[str]) -> tuple[str, str, str]:
    """(install command, the prefix for `<prefix> build`, package manager)."""
    declared = pkg.get("packageManager") if isinstance(pkg.get("packageManager"), str) else ""
    if "pnpm-lock.yaml" in lockfiles or declared.startswith("pnpm"):
        # Frozen only against a committed lockfile; without one pnpm must write it.
        frozen = " --frozen-lockfile" if "pnpm-lock.yaml" in lockfiles else ""
        return f"{_COREPACK} pnpm install{frozen}", f"{_COREPACK} pnpm run", "pnpm"
    if "yarn.lock" in lockfiles or declared.startswith("yarn"):
        # Modern Yarn defaults to Plug'n'Play, which a plain `node <bin>` launch can't
        # resolve; a node_modules install works for any launch command (Yarn 1 ignores it).
        return (
            f"export YARN_NODE_LINKER=node-modules\n{_COREPACK} yarn install",
            f"{_COREPACK} yarn run",
            "yarn",
        )
    if lockfiles & {"package-lock.json", "npm-shrinkwrap.json"}:
        return "npm ci", "npm run", "npm"
    return "npm install", "npm run", "npm"


def node_draft(
    index: int, ref: RepoRef, pkg: dict[str, Any], lockfiles: set[str], notes: list[str]
) -> dict[str, Any]:
    """A ``package.json`` → install + build steps and a ``node`` start command."""
    draft = _repo_draft(index, "npm", ref)
    install, run, pm = _node_install(pkg, lockfiles)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    steps = [install]
    # npm and pnpm run `prepare` on install, so a package whose `prepare` runs its build
    # needs no second one. A `prepare` that does something else (installing git hooks)
    # doesn't count, and modern Yarn never runs `prepare` on install, so Yarn always builds.
    prepare = scripts.get("prepare") if isinstance(scripts.get("prepare"), str) else ""
    builds_on_install = pm != "yarn" and "build" in prepare
    if "build" in scripts and not builds_on_install:
        steps.append(f"{run} build")
    draft["post_clone_script"] = "\n".join(steps)

    bins = _bins(pkg)
    name = pkg.get("name") if isinstance(pkg.get("name"), str) else ""
    main = pkg.get("main") if isinstance(pkg.get("main"), str) else ""
    if bins:
        # The bin npx itself would pick: the only one, or the one named after the package.
        chosen = _unscoped(name) if _unscoped(name) in bins else next(iter(bins))
        if len(bins) > 1 and chosen != _unscoped(name):
            notes.append(
                f"The package has several commands ({', '.join(sorted(bins))}); `{chosen}` "
                "was picked — change the arguments to run another."
            )
        target = _rel(bins[chosen])
        if target.endswith(_JS_EXT):
            draft["command"], draft["args"] = "node", [target]
        else:
            draft["command"], draft["args"] = f"./{target}", []
    elif "start" in scripts:
        # npm runs a script whichever package manager installed the project; --silent
        # keeps its "> pkg start" banner off stdout, the MCP channel.
        draft["command"], draft["args"] = "npm", ["run", "--silent", "start"]
    elif main:
        draft["command"], draft["args"] = "node", [_rel(main)]
    else:
        draft["reason"] = (
            "Couldn't tell how to start it: package.json has no `bin`, `start` script or "
            "`main`. Enter the command below (it runs inside the checkout)."
        )
        return draft
    draft["installable"] = True
    return draft


def _python_scripts(pyproject: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(project name, scripts) from [project], else from a legacy [tool.poetry] table."""
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    scripts = project.get("scripts")
    name = project.get("name")
    if not isinstance(scripts, dict) or not scripts:
        # Poetry declares both outside [project] on older projects.
        tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
        poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
        scripts = poetry.get("scripts")
        name = name or poetry.get("name")
    clean = {str(k): v for k, v in (scripts or {}).items() if isinstance(k, str) and k} \
        if isinstance(scripts, dict) else {}
    return (name if isinstance(name, str) else ""), clean


def python_draft(
    index: int, ref: RepoRef, pyproject: dict[str, Any], lockfiles: set[str], notes: list[str]
) -> dict[str, Any]:
    """A ``pyproject.toml`` → ``uv sync`` and ``uv run --no-sync <script>``."""
    draft = _repo_draft(index, "pypi", ref)
    # `uv sync` needs a [project] table. A legacy Poetry project (metadata only under
    # [tool.poetry]) still builds through its PEP 517 backend, so it's installed into a
    # plain .venv and launched from there instead.
    legacy = not isinstance(pyproject.get("project"), dict)
    if legacy:
        draft["post_clone_script"] = "uv venv --allow-existing\nuv pip install ."
    else:
        draft["post_clone_script"] = "uv sync --frozen" if "uv.lock" in lockfiles else "uv sync"
    name, scripts = _python_scripts(pyproject)
    if not scripts:
        draft["reason"] = (
            "Couldn't tell how to start it: pyproject.toml declares no [project.scripts] "
            "entry point. Enter the command below (it runs inside the checkout, e.g. "
            "`uv run --no-sync python server.py`)."
        )
        return draft
    if len(scripts) == 1:
        chosen = next(iter(scripts))
    elif name in scripts:
        chosen = name
    else:
        chosen = next(iter(scripts))
        notes.append(
            f"The package has several commands ({', '.join(sorted(scripts))}); `{chosen}` was "
            "picked — change the last argument to run another."
        )
    if legacy:
        draft["command"], draft["args"] = f".venv/bin/{chosen}", []
        # Detected, but from the older format: review before it starts on its own.
        draft["warnings"].append(
            "This is a legacy Poetry project (no [project] table), so it's installed with "
            "`uv pip install .` instead of `uv sync` — check the post-clone script and "
            "command before starting it."
        )
    else:
        # --no-sync: the post-clone script already synced; a start must not re-resolve.
        draft["command"], draft["args"] = "uv", ["run", "--no-sync", chosen]
    draft["installable"] = True
    return draft


def _description(*docs: dict[str, Any]) -> str:
    for doc in docs:
        desc = doc.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
    return ""


def repo_detail(
    ref: RepoRef,
    package_json: str | None,
    pyproject_toml: str | None,
    lockfiles: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Map a repository's root manifests (raw text, ``None`` = absent) and which lockfiles
    exist to a CatalogDetail. Pure.

    A Node draft comes first when both manifests exist (a repo with a package.json is
    almost always a Node server with Python tooling, not the reverse); the SPA takes the
    first installable draft either way. There is always at least one draft carrying the
    repository, so even an undetectable repo reviews as "fill in the command".
    """
    lockfiles = set(lockfiles)
    notes: list[str] = []
    drafts: list[dict[str, Any]] = []
    pkg: dict[str, Any] = {}
    project: dict[str, Any] = {}

    if package_json is not None:
        try:
            loaded = json.loads(package_json)
            pkg = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            notes.append("package.json at the repository root isn't valid JSON.")
        if pkg:
            drafts.append(node_draft(len(drafts), ref, pkg, lockfiles, notes))
    if pyproject_toml is not None:
        try:
            pyproject = tomllib.loads(pyproject_toml)
        except tomllib.TOMLDecodeError:
            notes.append("pyproject.toml at the repository root isn't valid TOML.")
            pyproject = {}
        if pyproject:
            project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
            drafts.append(python_draft(len(drafts), ref, pyproject, lockfiles, notes))

    where = f"{ref.owner}/{ref.repo}" + (f" @ {ref.ref}" if ref.ref else "")
    if not drafts:
        draft = _repo_draft(0, "unknown", ref)
        draft["reason"] = (
            f"No package.json or pyproject.toml at the root of {where}, so the build and "
            "start steps couldn't be detected (or the repository is private, or the branch "
            "doesn't exist). Enter the post-clone script and command below."
        )
        drafts.append(draft)
    notes.insert(0, _FIRST_START_NOTE)

    return {
        "source": SOURCE_ID,
        "manual_install": not any(d["installable"] for d in drafts),
        "notes": notes,
        "server": {
            "name": f"{ref.owner}/{ref.repo}",
            "title": ref.repo,
            "description": _description(pkg, project),
            "version": ref.ref,
            "status": "active",
            "repository_url": f"https://github.com/{ref.owner}/{ref.repo}"
            + (f"/tree/{ref.ref}" if ref.ref else ""),
            "web_url": None,
        },
        "drafts": drafts,
        "remotes": [],
    }


def raw_url(ref: RepoRef, path: str) -> str:
    # HEAD resolves to the default branch on raw.githubusercontent.com.
    at = quote(ref.ref, safe="/") if ref.ref else "HEAD"
    return f"{RAW_BASE}/{ref.owner}/{ref.repo}/{at}/{path}"


def _upstream_error(url: str, exc: httpx.HTTPError) -> CatalogUpstreamError:
    reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    logger.warning("repository fetch %s failed: %s", url, reason)
    return CatalogUpstreamError(reason)


async def fetch_text(http: httpx.AsyncClient, url: str) -> str | None:
    """GET a raw file: its text, ``None`` when absent (404), or ``CatalogUpstreamError``."""
    try:
        async with http.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as resp:
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                logger.warning("repository fetch %s returned %s", url, resp.status_code)
                raise CatalogUpstreamError(f"upstream returned {resp.status_code}")
            # Streamed, so an oversized file is abandoned at the cap rather than buffered.
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_MANIFEST_BYTES:
                    return None
            return body.decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as exc:
        raise _upstream_error(url, exc) from exc


async def file_exists(http: httpx.AsyncClient, url: str) -> bool:
    """HEAD a raw file (a lockfile can be megabytes; only its presence matters).
    Best effort: an error reads as absent, which only costs a less specific install step."""
    try:
        resp = await http.head(url, timeout=_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def get_repository(http: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """Resolve a pasted repository URL to a CatalogDetail (``ValueError`` on a bad URL)."""
    ref = parse_repo_url(url)
    package_json, pyproject_toml, *present = await asyncio.gather(
        fetch_text(http, raw_url(ref, "package.json")),
        fetch_text(http, raw_url(ref, "pyproject.toml")),
        *(file_exists(http, raw_url(ref, name)) for name in LOCKFILES),
    )
    lockfiles = {name for name, there in zip(LOCKFILES, present) if there}
    return repo_detail(ref, package_json, pyproject_toml, lockfiles)
