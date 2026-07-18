"""Runner seam tests — deterministic spec building; docker gated until M7."""

from __future__ import annotations

import pytest

from app.db.models import Server
from app.runners import build_spec
from app.util import new_id


def _server(**kw) -> Server:
    base = dict(
        id=new_id(), slug="s", name="s", runner="npx",
        command="npx", args=["-y", "pkg"], env={},
    )
    base.update(kw)
    return Server(**base)


def test_build_spec_is_deterministic():
    s = _server(env={"TOKEN": "abc"})
    assert build_spec(s) == build_spec(s)  # frozen dataclass equality


def test_build_spec_passthrough():
    s = _server(
        command="npx",
        args=["-y", "@scope/pkg", "--flag"],
        env={"K": "v"},
        cwd="/tmp",
        setup_script="echo ready",
    )
    spec = build_spec(s)
    assert spec.command == "npx"
    assert spec.args == ["-y", "@scope/pkg", "--flag"]
    assert spec.env == {"K": "v"}
    assert spec.cwd == "/tmp"
    assert spec.setup_script == "echo ready"


@pytest.mark.parametrize("runner", ["npx", "uvx", "command"])
def test_known_runners_build(runner):
    spec = build_spec(_server(runner=runner, command="x"))
    assert spec.command == "x"


def test_docker_runner_builds_hardened_spec():
    s = _server(
        runner="docker",
        command="ghcr.io/github/github-mcp-server",  # image ref
        args=[],  # container args
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "x"},
    )
    spec = build_spec(s)
    assert spec.command == "docker"
    assert spec.minimal_env is True  # docker child gets a scrubbed env (no elevator secrets)
    assert spec.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "x"}
    a = spec.args
    # hardened, cleanup, and reaping flags are present
    for flag in ("run", "-i", "--rm", "--init", "--cap-drop", "--security-opt", "--pids-limit"):
        assert flag in a
    assert "no-new-privileges" in a
    # secret passed by NAME only — never as KEY=value (keeps it out of argv/ps/inspect)
    assert ["-e", "GITHUB_PERSONAL_ACCESS_TOKEN"] == a[a.index("-e"):a.index("-e") + 2]
    assert "GITHUB_PERSONAL_ACCESS_TOKEN=x" not in a
    # image is last (before any container args), the container gets no args here
    assert a[-1] == "ghcr.io/github/github-mcp-server"
    # deterministic
    assert build_spec(s).args == a


def test_docker_runner_appends_container_args_after_image():
    s = _server(runner="docker", command="img:1", args=["--transport", "stdio"], env={})
    a = build_spec(s).args
    assert a[-3:] == ["img:1", "--transport", "stdio"]


def test_docker_runner_places_run_args_after_defaults_before_image():
    s = _server(
        runner="docker", command="img:1", args=["serve"],
        run_args=["--name", "my-mcp", "--shm-size=1g", "--memory", "2g"],
    )
    a = build_spec(s).args
    # operator options land after the hardening defaults (so duplicates win, docker is
    # last-wins) and before the `--` + image + container args
    i = a.index("--name")
    assert a[i:i + 5] == ["--name", "my-mcp", "--shm-size=1g", "--memory", "2g"]
    assert i > a.index("--pids-limit")
    assert a[-3:] == ["--", "img:1", "serve"]
    # the default --memory still precedes the operator override
    assert a.index("--memory") < i


def test_docker_runner_sanitizes_forbidden_run_args_on_legacy_rows():
    # The service rejects these at the boundary; a hand-edited row must still never
    # leak a value into argv, detach, spoof the reap label, or shift the image.
    s = _server(
        runner="docker", command="img:1", args=[],
        run_args=[
            "-e", "SECRET=x", "-ite", "SECRET2=y", "--detach", "-itd", "--",
            "stray-image", "--label", "mcpelevator.server=spoof",
            "-l=mcpelevator.server=spoof2", "--label-file", "/labels",
            "--name", "kept",
        ],
    )
    a = build_spec(s).args
    assert "SECRET=x" not in a and "SECRET2=y" not in a
    assert "--detach" not in a and "-itd" not in a and "-ite" not in a
    assert "mcpelevator.server=spoof" not in a and "-l=mcpelevator.server=spoof2" not in a
    assert "--label-file" not in a and "/labels" not in a
    assert "stray-image" not in a  # an unconsumed positional would displace the image
    assert a.count("--") == 1  # only the builder's own end-of-options marker
    i = a.index("--name")
    assert a[i:i + 2] == ["--name", "kept"]
    assert a[-2:] == ["--", "img:1"]


def test_docker_child_env_strips_proxy_and_reserved_vars(monkeypatch):
    # The docker CLI child gets: operator DOCKER_* + PATH/HOME from the bridge env, plus the
    # server's NON-reserved vars — but NOT a server-declared proxy var (it would reroute the
    # control-plane's own daemon request on a TCP DOCKER_HOST) nor a server DOCKER_* override.
    from app.bridge.host import _child_env

    monkeypatch.setenv("DOCKER_HOST", "tcp://dind:2375")
    monkeypatch.setenv("HTTP_PROXY", "http://operator-proxy:3128")  # operator's own — must NOT leak to CLI
    monkeypatch.setenv("MCPE_ADMIN_TOKEN", "secret")  # elevator secret — must never reach the child
    spec = {
        "minimal_env": True,
        "env": {"GITHUB_TOKEN": "x", "HTTP_PROXY": "http://evil:3128", "DOCKER_HOST": "tcp://evil"},
    }
    child = _child_env(spec)
    assert child["GITHUB_TOKEN"] == "x"          # normal server var kept
    assert child["DOCKER_HOST"] == "tcp://dind:2375"  # operator's DOCKER_HOST wins, not the server's
    assert "HTTP_PROXY" not in child             # neither the server's NOR the operator's proxy reaches the CLI
    assert "MCPE_ADMIN_TOKEN" not in child       # elevator secret never leaks


def test_local_exec_child_env_scrubs_control_plane_secrets(monkeypatch):
    # A passthrough (npx/uvx/command) child inherits ordinary tooling env (PATH, proxy, CA) but
    # NEVER the control plane's own ``MCPE_*`` secrets — so even a server that shells out to docker
    # or abuses BASH_ENV can't read the elevator's admin token. Server vars still win.
    from app.bridge.host import _child_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")     # tooling env — kept so npx can fetch
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ca.pem")   # kept so TLS works
    monkeypatch.setenv("MCPE_ADMIN_TOKEN", "secret")           # elevator secret — must be scrubbed
    monkeypatch.setenv("MCPE_SESSION_SECRET", "sign")          # elevator secret — must be scrubbed
    child = _child_env({"env": {"GITHUB_TOKEN": "x", "MCPE_CUSTOM": "operator"}})
    assert child["PATH"] == "/usr/bin"
    assert child["HTTPS_PROXY"] == "http://proxy:3128"
    assert child["NODE_EXTRA_CA_CERTS"] == "/etc/ca.pem"
    assert child["GITHUB_TOKEN"] == "x"                        # server var kept
    assert "MCPE_ADMIN_TOKEN" not in child                    # elevator secret never leaks
    assert "MCPE_SESSION_SECRET" not in child
    assert child["MCPE_CUSTOM"] == "operator"                 # a server-set MCPE_ var is the operator's own


def test_remote_runner_maps_url_transport_headers():
    s = _server(
        runner="remote",
        command="https://up.example/mcp",
        args=["sse"],
        env={"Authorization": "Bearer t"},
    )
    spec = build_spec(s)
    assert spec.command == "https://up.example/mcp"  # upstream URL
    assert spec.transport == "sse"  # from args[0]
    assert spec.env == {"Authorization": "Bearer t"}  # upstream headers
    assert spec.setup_script == ""


def test_remote_runner_defaults_transport_when_args_empty():
    spec = build_spec(_server(runner="remote", command="https://up.example/mcp", args=[]))
    assert spec.transport == "streamable-http"


def test_local_runner_transport_is_stdio():
    # The discriminator defaults so existing runners are unchanged.
    assert build_spec(_server(runner="npx", command="npx")).transport == "stdio"
