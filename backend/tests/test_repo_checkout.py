"""Repository checkouts (app.runners.repo) — URL normalization, the generated checkout
script run against a real local git upstream, a full unit activation launching from the
checkout, and the API round trip.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import LOOPBACK

from app.config import get_settings
from app.db.models import Server
from app.main import app
from app.runners import repo as repo_checkout
from app.runners.repo import checkout_script, display_url, normalize_repo

FIXTURE = Path(__file__).with_name("stdio_server_fixture.py")


@pytest.mark.parametrize(
    "url, ref, expected",
    [
        ("", "main", ("", "")),
        ("ooples/mcp-console-automation", "", ("https://github.com/ooples/mcp-console-automation", "")),
        ("github:o/r.git", "v1", ("https://github.com/o/r", "v1")),
        ("https://github.com/o/r.git", "", ("https://github.com/o/r", "")),
        ("https://www.github.com/o/r/tree/feature/x", "", ("https://github.com/o/r", "feature/x")),
        ("https://github.com/o/r/tree/main", "v2", ("https://github.com/o/r", "v2")),
        ("https://github.com/o/r#readme", "", ("https://github.com/o/r", "")),
        ("https://tok@github.com/o/private", "", ("https://tok@github.com/o/private", "")),
        ("git+https://gitlab.com/g/sub/r.git", "", ("https://gitlab.com/g/sub/r.git", "")),
        ("git@github.com:o/r.git", "", ("git@github.com:o/r.git", "")),
        ("ssh://git@host.example/r.git", "", ("ssh://git@host.example/r.git", "")),
    ],
)
def test_normalize_repo(url, ref, expected):
    assert normalize_repo(url, ref) == expected


@pytest.mark.parametrize(
    "url, ref, needle",
    [
        ("file:///etc", "", "must be an https"),
        ("ext::sh -c id", "", "single git URL"),
        ("--upload-pack=x", "", "single git URL"),
        ("/srv/repo", "", "must be an https"),
        ("https://github.com/o", "", "expected https://github.com"),
        ("o/r", "-x", "unsupported"),
        ("o/r", "a/../b", "unsupported"),
        ("o/r", "a b", "unsupported"),
    ],
)
def test_normalize_repo_refuses(url, ref, needle):
    with pytest.raises(ValueError, match=needle):
        normalize_repo(url, ref)


def test_display_url_hides_credentials():
    assert display_url("https://user:tok@github.com/o/r") == "https://github.com/o/r"
    assert display_url("git@github.com:o/r.git") == "git@github.com:o/r.git"


# ---- the generated script, against a real git upstream ------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q")
    (up / "server.py").write_text("v1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "v1")
    _git(up, "tag", "v1")
    return up


def _run(tmp_path: Path, url: str, ref: str = "", post: str = "") -> subprocess.CompletedProcess:
    script = checkout_script(
        url, ref, post, tmp_path / "repos" / "srv", tmp_path / "repos" / "srv.built"
    )
    (tmp_path / "repos").mkdir(exist_ok=True)
    return subprocess.run(
        ["/bin/sh", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True
    )


def test_checkout_clones_builds_once_and_rebuilds_on_change(tmp_path, upstream):
    checkout = tmp_path / "repos" / "srv"
    count = tmp_path / "builds"
    post = f"echo build >> {count}\ntest -f server.py"

    first = _run(tmp_path, str(upstream), post=post)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (checkout / "server.py").read_text() == "v1\n"
    assert count.read_text().count("build") == 1

    # A restart at the same commit with the same script doesn't rebuild.
    again = _run(tmp_path, str(upstream), post=post)
    assert again.returncode == 0, again.stderr
    assert "already built" in again.stdout
    assert count.read_text().count("build") == 1

    # A new upstream commit is fetched, checked out and rebuilt.
    (upstream / "server.py").write_text("v2\n")
    _git(upstream, "commit", "-qam", "v2")
    third = _run(tmp_path, str(upstream), post=post)
    assert third.returncode == 0, third.stderr
    assert (checkout / "server.py").read_text() == "v2\n"
    assert count.read_text().count("build") == 2

    # An edited post-clone script rebuilds the same commit.
    fourth = _run(tmp_path, str(upstream), post=post + "\ntrue")
    assert fourth.returncode == 0, fourth.stderr
    assert count.read_text().count("build") == 3

    # Pinning a tag goes back to it (and rebuilds there).
    pinned = _run(tmp_path, str(upstream), ref="v1", post=post)
    assert pinned.returncode == 0, pinned.stderr
    assert (checkout / "server.py").read_text() == "v1\n"
    assert count.read_text().count("build") == 4


def test_post_clone_never_targets_mcpelevators_own_virtualenv(tmp_path, upstream, monkeypatch):
    # Running under `uv run uvicorn …` exports VIRTUAL_ENV; a build must not install there.
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "control-plane-venv"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "control-plane-venv"))
    out = _run(
        tmp_path, str(upstream),
        post='test -z "${VIRTUAL_ENV:-}"\ntest -z "${UV_PROJECT_ENVIRONMENT:-}"',
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_failed_build_is_retried_on_the_next_start(tmp_path, upstream):
    flag = tmp_path / "fail"
    flag.write_text("")
    post = f"test ! -f {flag}"
    assert _run(tmp_path, str(upstream), post=post).returncode != 0
    flag.unlink()
    # No stamp was written for the failed build, so it runs again rather than skipping.
    ok = _run(tmp_path, str(upstream), post=post)
    assert ok.returncode == 0, ok.stderr
    assert "already built" not in ok.stdout


def test_unreachable_upstream_falls_back_to_the_existing_checkout(tmp_path, upstream):
    assert _run(tmp_path, str(upstream)).returncode == 0
    moved = tmp_path / "gone"
    upstream.rename(moved)
    # Same URL (the checkout's origin), now unreachable: start what's there.
    out = _run(tmp_path, str(upstream))
    assert out.returncode == 0, out.stderr
    assert "fetch failed; starting the existing checkout" in out.stdout


def test_no_checkout_and_no_upstream_fails(tmp_path):
    out = _run(tmp_path, str(tmp_path / "missing"))
    assert out.returncode != 0
    assert "no earlier checkout" in out.stderr


def test_changed_url_reclones(tmp_path, upstream):
    assert _run(tmp_path, str(upstream)).returncode == 0
    other = tmp_path / "other"
    shutil.copytree(upstream, other)
    (other / "server.py").write_text("other\n")
    _git(other, "commit", "-qam", "other")
    out = _run(tmp_path, str(other))
    assert out.returncode == 0, out.stderr
    assert "cloning" in out.stdout
    assert (tmp_path / "repos" / "srv" / "server.py").read_text() == "other\n"


def test_script_quotes_hostile_values(tmp_path):
    script = checkout_script(
        "https://x/$(touch pwned)", "", "echo ok", tmp_path / "d'q", tmp_path / "s"
    )
    assert "'https://x/$(touch pwned)'" in script


# ---- a unit launching from its checkout --------------------------------------------


async def test_unit_launches_from_the_checkout(tmp_path, monkeypatch, upstream):
    from test_unit import _port, _settings, _wait_for

    from app.supervisor import unit as unit_module
    from app.supervisor.unit import ServerUnit

    shutil.copy(FIXTURE, upstream / "server.py")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-qm", "fixture")
    monkeypatch.setattr(
        unit_module, "get_settings",
        lambda: _settings(tmp_path, start_timeout_s=60, repo_build_timeout_s=60),
    )
    checkout = repo_checkout.checkout_dir(tmp_path, "repo-unit")
    server = Server(
        id="repo-unit", slug="repo-unit", name="Repo unit", runner="command",
        command=sys.executable, args=["server.py"],
        # The fixture insists on its cwd and on a file the "build" produced.
        env={"EXPECTED_CWD": str(checkout)},
        repo_url=str(upstream), post_clone_script="touch setup-complete",
        config_hash="hash",
    )
    unit = ServerUnit(server)
    assert unit.spec.cwd == str(checkout)
    await unit.start(_port())
    try:
        await _wait_for(lambda: unit.state in {"running", "failed"}, timeout=30)
        assert unit.state == "running", "\n".join(unit.logs.snapshot())
        logs = unit.logs.snapshot()
        assert any("attempt 1/2: checkout" in line for line in logs)
        assert any("running post-clone script" in line for line in logs)
        assert [t["name"] for t in unit.tools] == ["ping"]
    finally:
        await unit.stop()


def test_relative_cwd_is_taken_inside_the_checkout(tmp_path, monkeypatch):
    from test_unit import _settings

    from app.supervisor import unit as unit_module
    from app.supervisor.unit import ServerUnit

    monkeypatch.setattr(unit_module, "get_settings", lambda: _settings(tmp_path))
    server = Server(
        id="r", slug="r", name="r", runner="command", command="node", args=["x.js"], env={},
        cwd="packages/server", repo_url="https://github.com/o/r", config_hash="h",
    )
    assert ServerUnit(server).spec.cwd == str(
        repo_checkout.checkout_dir(tmp_path, "r") / "packages" / "server"
    )


# ---- API ----------------------------------------------------------------------------


def test_api_round_trip_validation_and_delete_cleanup():
    with TestClient(app) as c:
        created = c.post(
            "/api/servers",
            json={
                "name": "Console", "runner": "command", "command": "node",
                "args": ["dist/mcp/server.js"],
                "repo_url": "https://github.com/ooples/mcp-console-automation/tree/main",
                "post_clone_script": "npm ci",
                "source": "github:ooples/mcp-console-automation",
            },
            headers=LOOPBACK,
        )
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        checkout = repo_checkout.checkout_dir(get_settings().data_dir, server_id)
        try:
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK).json()
            assert detail["repo_url"] == "https://github.com/ooples/mcp-console-automation"
            assert detail["repo_ref"] == "main"  # lifted out of the pasted page URL
            assert detail["post_clone_script"] == "npm ci"
            assert detail["source"] == "github:ooples/mcp-console-automation"
            assert detail["mcpb_exportable"] is False
            hash_before = detail["config_hash"]

            # Editing the build steps is a launch change (new hash → restart + rebuild).
            patched = c.patch(
                f"/api/servers/{server_id}",
                json={"post_clone_script": "npm ci\nnpm run build", "repo_ref": "v1.1.5"},
                headers=LOOPBACK,
            )
            assert patched.status_code == 200, patched.text
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK).json()
            assert detail["post_clone_script"] == "npm ci\nnpm run build"
            assert detail["repo_ref"] == "v1.1.5"
            assert detail["config_hash"] != hash_before

            bad = c.patch(
                f"/api/servers/{server_id}",
                json={"runner": "remote", "command": "https://up.example/mcp"},
                headers=LOOPBACK,
            )
            assert bad.status_code == 400
            assert "local runners" in bad.json()["detail"]

            bad = c.patch(
                f"/api/servers/{server_id}", json={"repo_url": "file:///etc"}, headers=LOOPBACK
            )
            assert bad.status_code == 400

            # Supplying a post-clone script while dropping the repo is still refused.
            bad = c.patch(
                f"/api/servers/{server_id}",
                json={"repo_url": "", "post_clone_script": "npm ci"},
                headers=LOOPBACK,
            )
            assert bad.status_code == 400 and "repository URL" in bad.json()["detail"]

            # Dropping the repository alone clears its ref and build steps with it.
            cleared = c.patch(
                f"/api/servers/{server_id}", json={"repo_url": ""}, headers=LOOPBACK
            )
            assert cleared.status_code == 200, cleared.text
            detail = c.get(f"/api/servers/{server_id}", headers=LOOPBACK).json()
            assert (detail["repo_url"], detail["repo_ref"], detail["post_clone_script"]) == (
                "", "", ""
            )

            checkout.mkdir(parents=True)
            (checkout / "x").write_text("x")
        finally:
            assert c.delete(f"/api/servers/{server_id}", headers=LOOPBACK).status_code == 204
        assert not checkout.exists()


def test_api_refuses_a_post_clone_script_without_a_repo_and_repos_on_remote():
    with TestClient(app) as c:
        r = c.post(
            "/api/servers",
            json={"name": "x", "runner": "command", "command": "node", "post_clone_script": "npm ci"},
            headers=LOOPBACK,
        )
        assert r.status_code == 400 and "repository URL" in r.json()["detail"]
        r = c.post(
            "/api/servers",
            json={"name": "x", "runner": "remote", "command": "https://up.example/mcp",
                  "repo_url": "o/r"},
            headers=LOOPBACK,
        )
        assert r.status_code == 400 and "local runners" in r.json()["detail"]


def test_api_refuses_docker_in_a_post_clone_script():
    with TestClient(app) as c:
        r = c.post(
            "/api/servers",
            json={"name": "x", "runner": "command", "command": "node", "repo_url": "o/r",
                  "post_clone_script": "docker run alpine", "enabled": True},
            headers=LOOPBACK,
        )
        assert r.status_code == 400 and "docker runner" in r.json()["detail"]


def test_remove_checkout_is_best_effort(tmp_path):
    checkout = repo_checkout.checkout_dir(tmp_path, "srv")
    (checkout / "node_modules").mkdir(parents=True)
    # A stamp path that can't be unlinked (here a directory) must not fail the delete.
    repo_checkout.stamp_path(tmp_path, "srv").mkdir()
    repo_checkout.remove_checkout(tmp_path, "srv")
    assert not checkout.exists()
    repo_checkout.remove_checkout(tmp_path, "never-existed")


def test_repo_build_timeout_must_be_positive():
    from pydantic import ValidationError

    from app.config import Settings

    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValidationError, match="repo_build_timeout_s"):
            Settings(repo_build_timeout_s=bad)
