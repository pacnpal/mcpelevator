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
    s = _server(command="npx", args=["-y", "@scope/pkg", "--flag"], env={"K": "v"}, cwd="/tmp")
    spec = build_spec(s)
    assert spec.command == "npx"
    assert spec.args == ["-y", "@scope/pkg", "--flag"]
    assert spec.env == {"K": "v"}
    assert spec.cwd == "/tmp"


@pytest.mark.parametrize("runner", ["npx", "uvx", "command"])
def test_known_runners_build(runner):
    spec = build_spec(_server(runner=runner, command="x"))
    assert spec.command == "x"


def test_docker_runner_is_gated():
    with pytest.raises(NotImplementedError):
        build_spec(_server(runner="docker", command="some/image"))
