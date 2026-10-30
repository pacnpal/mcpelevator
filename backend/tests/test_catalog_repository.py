"""Install from a GitHub repository — URL parsing, manifest → draft mapping, and the API.

The mapping is pure; the API test stubs ``repository.fetch_text`` (the module's only
outbound call), keyed by the raw-file URL it builds.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import LOOPBACK

from app.catalog import repository
from app.catalog.base import CatalogUpstreamError
from app.catalog.repository import RepoRef, parse_repo_url, repo_detail
from app.main import app


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/ooples/mcp-console-automation", RepoRef("ooples", "mcp-console-automation")),
        ("https://github.com/ooples/mcp-console-automation/", RepoRef("ooples", "mcp-console-automation")),
        ("https://github.com/ooples/mcp-console-automation.git", RepoRef("ooples", "mcp-console-automation")),
        ("https://github.com/o/r#readme", RepoRef("o", "r")),
        ("https://github.com/o/r?tab=readme-ov-file", RepoRef("o", "r")),
        ("http://www.github.com/o/r", RepoRef("o", "r")),
        ("github.com/o/r", RepoRef("o", "r")),
        ("  o/r  ", RepoRef("o", "r")),
        ("https://github.com/o/r/tree/v1.2.0", RepoRef("o", "r", "v1.2.0")),
        ("https://github.com/o/r/commit/abc123", RepoRef("o", "r", "abc123")),
        ("https://github.com/o/r/releases/tag/v2", RepoRef("o", "r", "v2")),
        ("git+https://github.com/o/r.git", RepoRef("o", "r")),
        # SSH and credentialed https keep their transport (a private repository clones
        # with them), rebuilt from owner/repo so a /tree suffix never leaks into the clone.
        ("git@github.com:o/r.git", RepoRef("o", "r", None, "git@github.com:o/r.git")),
        ("ssh://git@github.com/o/r.git", RepoRef("o", "r", None, "ssh://git@github.com/o/r.git")),
        ("ssh://git@github.com:2222/o/r.git",
         RepoRef("o", "r", None, "ssh://git@github.com:2222/o/r.git")),
        ("https://tok@github.com:8443/o/r", RepoRef("o", "r", None, "https://tok@github.com:8443/o/r")),
        ("https://tok@github.com/o/r/tree/v1", RepoRef("o", "r", "v1", "https://tok@github.com/o/r")),
        ("git+https://u:p@github.com/o/r.git", RepoRef("o", "r", None, "https://u:p@github.com/o/r")),
        ("github:o/r", RepoRef("o", "r")),
        ("github:o/r#feature/x", RepoRef("o", "r", "feature/x")),
    ],
)
def test_parse_repo_url_accepts_what_operators_paste(url, expected):
    assert parse_repo_url(url) == expected


@pytest.mark.parametrize(
    "url, needle",
    [
        ("", "enter"),
        ("https://gitlab.com/o/r", "only GitHub"),
        ("https://evil.example/o/r", "only GitHub"),
        ("https://github.com/o", "expected a repository"),
        ("https://github.com/o/r/tree/main/packages/fs", "subdirectory"),
        ("https://github.com/o/r/blob/main/README.md", "subdirectory"),
        ("github:o/r#../../x", "unsupported"),
        ("github:o/r#-x", "unsupported"),
        ("https://github.com/-bad/r", "not a GitHub repository"),
        ("https://github.com/o/..", "not a GitHub repository"),
    ],
)
def test_parse_repo_url_rejects_with_a_reason(url, needle):
    with pytest.raises(ValueError, match=needle):
        parse_repo_url(url)


def _pkg(**over):
    pkg = {
        "name": "console-automation-mcp",
        "description": "console automation",
        "bin": {"console-automation-mcp": "./dist/mcp/server.js", "mcp-console": "./dist/mcp/server.js"},
        "scripts": {"build": "tsc", "prepare": "npm run build"},
    }
    pkg.update(over)
    return json.dumps(pkg)


def test_node_repo_detects_install_build_and_start():
    """The motivating case: a repo whose README says clone + build, never published to
    npm (and whose `npx -y github:…` form fails). It becomes a command server launched
    from a checkout: `npm ci` (its lockfile; `prepare` builds it), then the bin via node."""
    detail = repo_detail(
        RepoRef("ooples", "mcp-console-automation"), _pkg(), None, {"package-lock.json"}
    )
    assert detail["manual_install"] is False
    assert detail["server"]["name"] == "ooples/mcp-console-automation"
    assert detail["server"]["title"] == "mcp-console-automation"
    assert detail["server"]["description"] == "console automation"
    (draft,) = detail["drafts"]
    assert (draft["runner"], draft["command"], draft["args"]) == (
        "command", "node", ["dist/mcp/server.js"]
    )
    assert draft["repo_url"] == "https://github.com/ooples/mcp-console-automation"
    assert draft["repo_ref"] == ""
    assert draft["post_clone_script"] == "npm ci"
    assert draft["installable"] is True and draft["warnings"] == []
    assert any("post-clone" in n for n in detail["notes"])


def test_draft_keeps_the_pasted_clone_transport():
    ref = parse_repo_url("git@github.com:o/private.git")
    draft = repo_detail(ref, _pkg(), None)["drafts"][0]
    assert draft["repo_url"] == "git@github.com:o/private.git"


def test_node_ref_is_carried():
    detail = repo_detail(RepoRef("o", "r", "v1.2.0"), _pkg(), None)
    assert detail["drafts"][0]["repo_ref"] == "v1.2.0"
    assert detail["server"]["version"] == "v1.2.0"
    assert detail["server"]["repository_url"] == "https://github.com/o/r/tree/v1.2.0"


_COREPACK = "COREPACK_ENABLE_DOWNLOAD_PROMPT=0 npx -y corepack"


@pytest.mark.parametrize(
    "over, lockfiles, script",
    [
        ({}, set(), "npm install"),
        # A `prepare` that doesn't build (git hooks) doesn't stand in for the build.
        ({"scripts": {"build": "tsc", "prepare": "husky install"}}, {"package-lock.json"},
         "npm ci\nnpm run build"),
        ({"scripts": {"build": "tsc"}}, {"package-lock.json"}, "npm ci\nnpm run build"),
        # npm and pnpm build through `prepare` on install, so no explicit step.
        ({}, {"pnpm-lock.yaml"}, f"{_COREPACK} pnpm install --frozen-lockfile"),
        # Declared pnpm without a committed lockfile: pnpm must be free to write one.
        ({"packageManager": "pnpm@9.1.0"}, set(), f"{_COREPACK} pnpm install"),
        # Yarn installs a node_modules tree (not Plug'n'Play) so `node <bin>` resolves.
        ({"scripts": {"build": "tsc"}}, {"yarn.lock"},
         f"export YARN_NODE_LINKER=node-modules\n{_COREPACK} yarn install\n"
         f"{_COREPACK} yarn run build"),
        # Modern Yarn never runs `prepare` on install, so it builds even with one.
        ({"scripts": {"build": "tsc", "prepare": "npm run build"}}, {"yarn.lock"},
         f"export YARN_NODE_LINKER=node-modules\n{_COREPACK} yarn install\n"
         f"{_COREPACK} yarn run build"),
    ],
)
def test_node_install_steps_follow_the_lockfile(over, lockfiles, script):
    draft = repo_detail(RepoRef("o", "r"), _pkg(**over), None, lockfiles)["drafts"][0]
    assert draft["post_clone_script"] == script


def test_node_start_command_fallbacks():
    # npm runs the start script even for a pnpm project (no corepack at launch).
    start = repo_detail(
        RepoRef("o", "r"), _pkg(bin=None, scripts={"start": "node x"}), None, {"pnpm-lock.yaml"}
    )
    assert start["drafts"][0]["command"] == "npm"
    assert start["drafts"][0]["args"] == ["run", "--silent", "start"]
    main = repo_detail(RepoRef("o", "r"), _pkg(bin=None, scripts={}, main="./index.js"), None)
    assert (main["drafts"][0]["command"], main["drafts"][0]["args"]) == ("node", ["index.js"])
    shell = repo_detail(RepoRef("o", "r"), _pkg(bin="bin/serve"), None)
    assert (shell["drafts"][0]["command"], shell["drafts"][0]["args"]) == ("./bin/serve", [])


def test_node_ambiguous_bins_pick_one_with_a_note():
    detail = repo_detail(RepoRef("o", "r"), _pkg(name="srv", bin={"a": "a.js", "b": "b.js"}), None)
    assert detail["drafts"][0]["args"] == ["a.js"]
    assert any("several commands" in n for n in detail["notes"])


def test_node_without_a_start_is_editable_not_dropped():
    """Detection failing still hands over the checkout and install steps to finish by hand."""
    detail = repo_detail(RepoRef("o", "r"), _pkg(bin=None, scripts={}), None)
    assert detail["manual_install"] is True
    draft = detail["drafts"][0]
    assert draft["installable"] is False and "Couldn't tell how to start" in draft["reason"]
    assert draft["repo_url"] == "https://github.com/o/r"
    assert draft["post_clone_script"] == "npm install"
    assert (draft["runner"], draft["command"]) == ("command", "")


_PYPROJECT = """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[project]
name = "weather-mcp"
description = "weather"
[project.scripts]
weather-mcp = "weather.server:main"
weather-admin = "weather.admin:main"
"""


def test_python_repo_maps_to_uv_sync_and_run():
    detail = repo_detail(RepoRef("o", "weather", "main"), None, _PYPROJECT, {"uv.lock"})
    (draft,) = detail["drafts"]
    assert (draft["runner"], draft["command"]) == ("command", "uv")
    # Several scripts: the one named after the project wins, silently.
    assert draft["args"] == ["run", "--no-sync", "weather-mcp"]
    assert draft["post_clone_script"] == "uv sync --frozen"
    assert (draft["repo_url"], draft["repo_ref"]) == ("https://github.com/o/weather", "main")
    assert draft["installable"] is True
    assert detail["server"]["description"] == "weather"
    assert not any("several commands" in n for n in detail["notes"])


def test_python_without_a_build_backend_waits_for_review():
    """uv installs a project's entry points only when it's a package (a [build-system],
    or tool.uv.package = true); otherwise `uv run <script>` has nothing to run."""
    text = '[project]\nname = "w"\n[project.scripts]\nw = "w:main"\n'
    draft = repo_detail(RepoRef("o", "r"), None, text)["drafts"][0]
    assert draft["args"] == ["run", "--no-sync", "w"]
    assert any("[build-system]" in w for w in draft["warnings"])
    packaged = text + "[tool.uv]\npackage = true\n"
    assert repo_detail(RepoRef("o", "r"), None, packaged)["drafts"][0]["warnings"] == []


def test_python_legacy_poetry_installs_into_a_venv_and_waits_for_review():
    """`uv sync` refuses a project with no [project] table, so a legacy Poetry project is
    built through its backend into .venv — flagged so it doesn't auto-start unreviewed."""
    text = '[tool.poetry]\nname = "p"\n[tool.poetry.scripts]\nserve = "p:main"\n'
    draft = repo_detail(RepoRef("o", "r"), None, text)["drafts"][0]
    assert draft["post_clone_script"] == (
        "uv venv --allow-existing\nuv pip install --python .venv/bin/python ."
    )
    assert (draft["command"], draft["args"]) == (".venv/bin/serve", [])
    assert draft["installable"] is True
    assert any("legacy Poetry" in w for w in draft["warnings"])
    # Still held for review when no start command could be detected.
    scriptless = repo_detail(RepoRef("o", "r"), None, '[tool.poetry]\nname = "p"\n')["drafts"][0]
    assert scriptless["installable"] is False
    assert any("legacy Poetry" in w for w in scriptless["warnings"])


def test_python_without_scripts_is_editable():
    detail = repo_detail(RepoRef("o", "r"), None, '[project]\nname = "x"\n')
    assert detail["manual_install"] is True
    assert "[project.scripts]" in detail["drafts"][0]["reason"]
    assert detail["drafts"][0]["post_clone_script"] == "uv sync"


def test_both_manifests_node_first():
    detail = repo_detail(RepoRef("o", "r"), _pkg(), _PYPROJECT)
    assert [d["registry_type"] for d in detail["drafts"]] == ["npm", "pypi"]
    assert [d["package_index"] for d in detail["drafts"]] == [0, 1]


def test_no_manifest_or_invalid_ones_still_carry_the_repo():
    detail = repo_detail(RepoRef("o", "r"), None, None)
    assert detail["manual_install"] is True
    draft = detail["drafts"][0]
    assert "No package.json or pyproject.toml" in draft["reason"]
    assert draft["repo_url"] == "https://github.com/o/r" and draft["runner"] == "command"
    detail = repo_detail(RepoRef("o", "r"), "{not json", "not = [toml")
    assert detail["manual_install"] is True
    assert any("valid JSON" in n for n in detail["notes"])
    assert any("valid TOML" in n for n in detail["notes"])


def test_raw_url_uses_head_for_the_default_branch():
    assert repository.raw_url(RepoRef("o", "r"), "package.json") == (
        "https://raw.githubusercontent.com/o/r/HEAD/package.json"
    )
    assert repository.raw_url(RepoRef("o", "r", "feature/x"), "package.json") == (
        "https://raw.githubusercontent.com/o/r/feature/x/package.json"
    )


def _stub_fetch(monkeypatch, files: dict[str, str | None] | Exception, lockfiles=()):
    calls: list[str] = []

    async def fake_fetch_text(http, url):
        calls.append(url)
        if isinstance(files, Exception):
            raise files
        return files.get(url.rsplit("/", 1)[-1])

    async def fake_file_exists(http, url):
        calls.append(url)
        return url.rsplit("/", 1)[-1] in lockfiles

    monkeypatch.setattr(repository, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(repository, "file_exists", fake_file_exists)
    return calls


def test_repository_endpoint_resolves_a_draft(monkeypatch):
    calls = _stub_fetch(monkeypatch, {"package.json": _pkg()}, lockfiles={"package-lock.json"})
    with TestClient(app) as c:
        r = c.get(
            "/api/catalog/repository",
            params={"url": "https://github.com/ooples/mcp-console-automation"},
            headers=LOOPBACK,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "github"
    draft = body["drafts"][0]
    assert (draft["command"], draft["args"]) == ("node", ["dist/mcp/server.js"])
    assert draft["post_clone_script"] == "npm ci"
    assert draft["repo_url"] == "https://github.com/ooples/mcp-console-automation"
    # Only ever raw.githubusercontent.com, built from the parsed parts.
    base = "https://raw.githubusercontent.com/ooples/mcp-console-automation/HEAD/"
    assert sorted(calls) == sorted(
        base + name for name in ("package.json", "pyproject.toml", *repository.LOCKFILES)
    )


def test_repository_endpoint_bad_url_is_400(monkeypatch):
    calls = _stub_fetch(monkeypatch, {})
    with TestClient(app) as c:
        r = c.get("/api/catalog/repository", params={"url": "https://gitlab.com/o/r"}, headers=LOOPBACK)
    assert r.status_code == 400
    assert "only GitHub" in r.json()["detail"]
    assert calls == []


def test_repository_endpoint_upstream_down_is_503(monkeypatch):
    _stub_fetch(monkeypatch, CatalogUpstreamError("ConnectError"))
    with TestClient(app) as c:
        r = c.get("/api/catalog/repository", params={"url": "o/r"}, headers=LOOPBACK)
    assert r.status_code == 503
    assert "GitHub unavailable" in r.json()["detail"]



def _client(handler):
    import httpx

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_text_statuses_and_size_cap(monkeypatch):
    import httpx

    monkeypatch.setattr(repository, "_MAX_MANIFEST_BYTES", 10)

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        return {
            "ok": httpx.Response(200, text="{}"),
            "missing": httpx.Response(404),
            "huge": httpx.Response(200, content=b"x" * 11),
            "down": httpx.Response(502),
        }[name]

    async with _client(handler) as http:
        assert await repository.fetch_text(http, "https://raw.example/ok") == "{}"
        assert await repository.fetch_text(http, "https://raw.example/missing") is None
        # Past the cap it's treated as absent, read no further than the cap.
        assert await repository.fetch_text(http, "https://raw.example/huge") is None
        with pytest.raises(CatalogUpstreamError):
            await repository.fetch_text(http, "https://raw.example/down")
