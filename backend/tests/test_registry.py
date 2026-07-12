"""Registry / SSOT tests — slug uniqueness and the config_hash idempotency anchor."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db import repo
from app.registry import service


@pytest.fixture
def session():
    from app.db import models  # noqa: F401 — register tables

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _mk(session, **kw):
    base = dict(name="Memory", runner="npx", command="npx", args=["-y", "pkg"])
    base.update(kw)
    return service.create_server(session, **base)


def test_unique_slugs(session):
    a = _mk(session, name="Memory", args=["-y", "x"])
    b = _mk(session, name="Memory", args=["-y", "y"])
    assert a.slug == "memory"
    assert b.slug == "memory-2"


def test_reserved_slug_is_not_assigned(session):
    """A server named "summary" must not get the slug "summary" — that would shadow
    the static /api/health/summary route so its own /api/health/{slug} is unreachable.
    It's disambiguated instead, leaving the reserved word free for the aggregate route."""
    a = _mk(session, name="summary")
    assert a.slug == "summary-2"
    # the reserved word stays free no matter how the name is cased/spaced
    b = _mk(session, name="Summary")
    assert b.slug == "summary-3"


def test_config_hash_changes_on_edit(session):
    a = _mk(session, args=["-y", "x"])
    before = a.config_hash
    service.update_server(session, a.id, {"args": ["-y", "z"]})
    after = repo.get_server(session, a.id).config_hash
    assert after != before


def test_config_hash_is_order_independent(session):
    """Same logical config -> same hash, regardless of env key order.

    This is the idempotency anchor: the reconciler must NOT restart a server when
    nothing meaningful changed.
    """
    a = _mk(session, env={"B": "2", "A": "1"})
    before = a.config_hash
    service.update_server(session, a.id, {"env": {"A": "1", "B": "2"}})
    after = repo.get_server(session, a.id).config_hash
    assert after == before


def test_unknown_runner_rejected(session):
    with pytest.raises(ValueError):
        service.create_server(session, name="x", runner="bogus", command="x")


def test_remote_server_canonicalizes_and_validates(session):
    s = service.create_server(
        session,
        name="Remote",
        runner="remote",
        command="https://up.example/mcp",
        args=["http"],  # alias → canonical streamable-http
        env={"Authorization": "Bearer t"},
    )
    assert s.runner == "remote"
    assert s.command == "https://up.example/mcp"
    assert s.args == ["streamable-http"]  # canonicalized for deterministic storage
    assert s.env == {"Authorization": "Bearer t"}


def test_remote_server_defaults_transport(session):
    s = service.create_server(session, name="R", runner="remote", command="https://x/mcp")
    assert s.args == ["streamable-http"]


def test_remote_server_rejects_non_url(session):
    with pytest.raises(ValueError):
        service.create_server(session, name="R", runner="remote", command="not-a-url")


def test_remote_server_accepts_uppercase_scheme(session):
    # URL schemes are case-insensitive — HTTPS:// must not be rejected.
    s = service.create_server(session, name="R", runner="remote", command="HTTPS://x/mcp")
    assert s.runner == "remote"


def test_remote_server_rejects_hostless_url(session):
    # "https://:443/mcp" has a netloc but no host — reject up front, not at connect time.
    with pytest.raises(ValueError):
        service.create_server(session, name="R", runner="remote", command="https://:443/mcp")


def test_remote_server_rejects_invalid_port(session):
    # A malformed port must be rejected at create time, not left to fail at readiness.
    with pytest.raises(ValueError):
        service.create_server(
            session, name="R", runner="remote", command="https://up.example:bad/mcp"
        )


def test_remote_server_rejects_bad_transport(session):
    with pytest.raises(ValueError):
        service.create_server(
            session, name="R", runner="remote", command="https://x/mcp", args=["websocket"]
        )


def test_remote_update_clears_stale_cwd(session):
    # Converting a local server (with a cwd) to remote must drop the now-meaningless cwd.
    s = service.create_server(
        session, name="L", runner="npx", command="npx", args=["-y", "p"], cwd="/tmp"
    )
    assert s.cwd == "/tmp"
    updated = service.update_server(
        session, s.id, {"runner": "remote", "command": "https://x/mcp", "args": ["sse"]}
    )
    assert updated.runner == "remote"
    assert updated.cwd is None


def test_remote_create_ignores_cwd(session):
    s = service.create_server(
        session, name="R", runner="remote", command="https://x/mcp", cwd="/tmp"
    )
    assert s.cwd is None


def test_remote_config_hash_is_deterministic(session):
    """Same logical remote config (alias-normalized) → same hash; a transport change
    re-hashes (drives one idempotent reconcile)."""
    a = service.create_server(
        session, name="A", runner="remote", command="https://x/mcp", args=["http"]
    )
    b = service.create_server(
        session, name="B", runner="remote", command="https://x/mcp", args=["streamable-http"]
    )
    assert a.config_hash == b.config_hash  # "http" alias collapses to the same spec
    before = a.config_hash
    service.update_server(session, a.id, {"args": ["sse"]})
    assert repo.get_server(session, a.id).config_hash != before


def test_slug_rename(session):
    a = _mk(session, name="Memory")
    assert a.slug == "memory"
    service.update_server(session, a.id, {"slug": "brain"})
    assert repo.get_server(session, a.id).slug == "brain"
    # the freed slug can now be reused by another server
    b = _mk(session, name="Memory")
    assert b.slug == "memory"


def test_slug_rename_is_normalized(session):
    a = _mk(session, name="Memory")
    service.update_server(session, a.id, {"slug": "My Cool Server!!"})
    assert repo.get_server(session, a.id).slug == "my-cool-server"


def test_slug_rename_rejects_collision(session):
    a = _mk(session, name="Alpha", args=["-y", "a"])
    _mk(session, name="Beta", args=["-y", "b"])
    with pytest.raises(ValueError):
        service.update_server(session, a.id, {"slug": "beta"})


def test_slug_rename_to_self_is_allowed(session):
    a = _mk(session, name="Memory")
    service.update_server(session, a.id, {"slug": "memory"})
    assert repo.get_server(session, a.id).slug == "memory"


def test_slug_rename_rejects_reserved(session):
    a = _mk(session, name="Memory")
    with pytest.raises(ValueError):
        service.update_server(session, a.id, {"slug": "summary"})


def test_slug_rename_does_not_restart(session):
    """Slug is routing/identity, not launch config — renaming it must not change
    config_hash (which would needlessly bounce the bridge)."""
    a = _mk(session, args=["-y", "x"])
    before = a.config_hash
    service.update_server(session, a.id, {"slug": "renamed"})
    assert repo.get_server(session, a.id).config_hash == before


def test_clone_server_copies_config(session):
    src = _mk(session, name="Memory", env={"K": "v"}, args=["-y", "pkg"])
    src = service.update_server(session, src.id, {"auth_provider": "bearer"})

    copy = service.clone_server(session, src.id)
    assert copy.id != src.id
    assert copy.slug != src.slug  # unique slug derived from the new name
    assert copy.name == "Memory copy"
    assert copy.runner == src.runner
    assert copy.command == src.command
    assert copy.args == src.args
    assert copy.env == src.env
    assert copy.auth_provider == src.auth_provider
    assert copy.enabled is False  # always created disabled
    assert copy.source == "clone"
    assert copy.config_hash == src.config_hash  # identical launch config -> same hash


def test_clone_server_custom_name(session):
    src = _mk(session, name="Memory")
    copy = service.clone_server(session, src.id, name="Memory (staging)")
    assert copy.name == "Memory (staging)"
    assert copy.slug == "memory-staging"


def test_clone_unknown_server(session):
    with pytest.raises(KeyError):
        service.clone_server(session, "nope")


# --- docker runner: normalization + the opt-in root-equivalent gate ------------


def _enable_docker(session):
    from app.registry import settings as runtime_settings

    runtime_settings.write(session, {"docker_runner": True})


def test_normalize_docker_parses_full_invocation():
    image, args, env, warnings = service.normalize_docker(
        "/usr/local/bin/docker",
        ["run", "-i", "--rm", "-e", "TOKEN", "ghcr.io/x/y", "--flag"],
        {"TOKEN": "v"},
    )
    assert image == "ghcr.io/x/y"
    assert args == ["--flag"]  # container args, after the image
    assert env == {"TOKEN": "v"}
    assert warnings == []


def test_normalize_docker_merges_inline_env_and_skips_value_flags():
    image, args, env, _ = service.normalize_docker(
        "docker",
        ["run", "-v", "/a:/b", "-e", "FOO=bar", "--network", "host", "img:1", "sub"],
        {},
    )
    assert image == "img:1"
    assert args == ["sub"]
    assert env == {"FOO": "bar"}  # inline -e VAR=val folded into the env map


def test_normalize_docker_warns_on_detach():
    _, _, _, warnings = service.normalize_docker("docker", ["run", "-d", "img"], {})
    assert any("detach" in w for w in warnings)


def test_normalize_docker_scaffolds_bare_env_passthrough():
    # A bare `-e SECRET` (host-env passthrough) with no value in the env object must be
    # scaffolded as SECRET="" (so it's emitted + reviewable), not silently dropped.
    image, args, env, warnings = service.normalize_docker("docker", ["run", "-e", "SECRET", "img"], {})
    assert image == "img"
    assert env == {"SECRET": ""}
    assert any("SECRET" in w for w in warnings)


def test_normalize_docker_skips_network_alias_value():
    # --network-alias takes a value; its value must not be mistaken for the image.
    image, args, env, _ = service.normalize_docker(
        "docker", ["run", "--network", "mynet", "--network-alias", "myalias", "img:1"], {}
    )
    assert image == "img:1" and args == []


def test_normalize_docker_preserves_image_named_docker():
    # A real image whose basename is "docker" (official image / ghcr.io/acme/docker) must
    # not be parsed as a CLI launcher just because the args don't start with `run`.
    img, args, _, _ = service.normalize_docker("docker", [], {})
    assert img == "docker" and args == []
    img2, args2, _, _ = service.normalize_docker("ghcr.io/acme/docker", ["--flag"], {})
    assert img2 == "ghcr.io/acme/docker" and args2 == ["--flag"]


def test_normalize_docker_servers_migrates_misclassified_command_row(session):
    # A legacy import of `/usr/local/bin/docker run …` stored as runner="command" must be
    # converted to the docker runner (so it's gated + hardened), with reserved env scrubbed.
    _enable_docker(session)
    s = service.create_server(
        session, name="legacycmd", runner="command", command="/usr/local/bin/docker",
        args=["run", "--rm", "-e", "T", "ghcr.io/x/y"], env={"T": "v"},
    )
    # Sneak a reserved key into the stored row (bypassing create validation via the repo).
    row = repo.get_server(session, s.id)
    row.env = {"T": "v", "DOCKER_HOST": "tcp://evil:2375"}
    repo.save_server(session, row)

    assert service.normalize_docker_servers(session) == 1
    m = repo.get_server(session, s.id)
    assert m.runner == "docker"
    assert m.command == "ghcr.io/x/y" and m.args == []
    assert m.env == {"T": "v"}  # reserved DOCKER_HOST scrubbed


def test_docker_rejects_leading_dash_image(session):
    # A `command` (image) starting with "-" would inject a docker run flag (host mount /
    # --privileged) — reject it at create so it can never persist.
    _enable_docker(session)
    with pytest.raises(ValueError):
        service.create_server(
            session, name="d", runner="docker", command="--volume=/:/host",
            args=["alpine"], env={}, enabled=False,
        )


def test_docker_build_emits_end_of_options_before_image():
    from app.db.models import Server
    from app.runners.docker import build
    from app.util import new_id
    s = Server(id=new_id(), slug="x", name="x", runner="docker", command="ghcr.io/x/y",
               args=["a"], env={})
    argv = build(s).args
    # `--` immediately precedes the image so a leading-dash image can't be parsed as a flag.
    assert argv[argv.index("--") + 1] == "ghcr.io/x/y"


def test_any_local_runner_named_docker_is_gated(session):
    # A passthrough runner (npx/uvx/command) pointed at the docker CLI must be routed through
    # the gated docker runner — choosing a different runner string must NOT sidestep the gate,
    # hardening, or minimal_env. (docker_runner is off by default here.)
    for rn in ("npx", "uvx", "command"):
        with pytest.raises(ValueError):
            service.create_server(
                session, name=f"x{rn}", runner=rn, command="docker",
                args=["run", "--privileged", "-v", "/:/host", "alpine"],
                env={"MCPE_ADMIN_TOKEN": ""}, enabled=True,
            )


def test_command_runner_named_docker_reclassifies_to_docker(session):
    # A `command` runner whose launcher is docker must be routed through the docker runner
    # (gated + hardened), not launched ungated via passthrough.
    s = service.create_server(
        session, name="c", runner="command", command="/usr/local/bin/docker",
        args=["run", "--rm", "-e", "T", "img:1"], env={"T": "v"}, enabled=False,
    )
    assert s.runner == "docker"
    assert s.command == "img:1" and s.env == {"T": "v"}
    # And it's now gated: enabling while the runner is off is refused.
    with pytest.raises(ValueError):
        service.set_enabled(session, s.id, True)


def test_edit_enabled_docker_server_allowed_while_runner_off(session):
    # An already-enabled docker server can be edited while the runner is off (fix a bad
    # image/env); the reconcile gate — not update — keeps it from running.
    _enable_docker(session)
    s = service.create_server(
        session, name="d", runner="docker", command="img:1", args=[], env={}, enabled=True
    )
    from app.registry import settings as runtime_settings
    runtime_settings.write(session, {"docker_runner": False})
    # Should NOT raise (previously a 400/ValueError).
    updated = service.update_server(session, s.id, {"command": "img:2"})
    assert updated.command == "img:2" and updated.enabled is True


def test_normalize_docker_attached_short_env():
    _, _, env, warnings = service.normalize_docker("docker", ["run", "-eGITHUB_TOKEN", "img"], {})
    assert env == {"GITHUB_TOKEN": ""} and any("GITHUB_TOKEN" in w for w in warnings)
    _, _, env2, _ = service.normalize_docker("docker", ["run", "-eFOO=bar", "img"], {})
    assert env2 == {"FOO": "bar"}


def test_normalize_docker_inline_env_file_warns():
    _, _, _, warnings = service.normalize_docker("docker", ["run", "--env-file=/a.env", "img"], {})
    assert any("env-file" in w for w in warnings)


def test_normalize_docker_global_flags_before_run():
    # `docker --context X run --rm img` — a global flag before the subcommand must still parse.
    image, args, _, _ = service.normalize_docker(
        "docker", ["--context", "x", "run", "--rm", "img"], {}
    )
    assert image == "img" and args == []


def test_normalize_docker_container_run_longform():
    # `docker container run …` is the canonical long form of `docker run …`.
    image, args, _, _ = service.normalize_docker(
        "docker", ["container", "run", "-i", "ghcr.io/x/y"], {}
    )
    assert image == "ghcr.io/x/y" and args == []


def test_normalize_docker_attach_is_value_flag():
    # `-a stdin -a stdout` take values; the image must not be mistaken for an attach target.
    image, args, _, _ = service.normalize_docker(
        "docker", ["run", "-a", "stdin", "-a", "stdout", "-i", "ghcr.io/x/y"], {}
    )
    assert image == "ghcr.io/x/y" and args == []


def test_normalize_docker_warns_on_dropped_mount():
    _, _, _, warnings = service.normalize_docker(
        "docker", ["run", "-v", "/host/data:/data", "img"], {}
    )
    assert any("mount" in w for w in warnings)
    # inline form too
    _, _, _, w2 = service.normalize_docker("docker", ["run", "--volume=/a:/b", "img"], {})
    assert any("mount" in w for w in w2)


def test_normalize_docker_servers_migrates_global_flag_command_row(session):
    # A legacy runner="command" row with a global flag before `run` must still migrate to docker.
    _enable_docker(session)
    s = service.create_server(
        session, name="g", runner="command", command="/usr/local/bin/docker",
        args=["--context", "prod", "run", "img"], env={},
    )
    # Force it back to the legacy command shape (create already reclassified it to docker).
    row = repo.get_server(session, s.id)
    row.runner = "command"
    row.command, row.args = "/usr/local/bin/docker", ["--context", "prod", "run", "img"]
    repo.save_server(session, row)

    assert service.normalize_docker_servers(session) == 1
    m = repo.get_server(session, s.id)
    assert m.runner == "docker" and m.command == "img"


def test_normalize_docker_warns_on_env_file():
    _, _, _, warnings = service.normalize_docker("docker", ["run", "--env-file", "s.env", "img"], {})
    assert any("env-file" in w for w in warnings)


def test_normalize_docker_handles_windows_launcher_path():
    # A Windows Claude-Desktop path must be recognized as the docker launcher on any OS.
    image, args, env, _ = service.normalize_docker(
        r"C:\Program Files\Docker\docker.exe", ["run", "--rm", "-e", "T", "img"], {"T": "v"}
    )
    assert image == "img" and env == {"T": "v"}


def test_docker_rejects_env_key_with_equals(session):
    # A key containing "=" would become `-e KEY=value` in argv (leaking the value). Reject it.
    _enable_docker(session)
    with pytest.raises(ValueError):
        service.create_server(
            session, name="d", runner="docker", command="img:1", args=[],
            env={"FOO=leaked": ""}, enabled=False,
        )


def test_docker_rejects_reserved_env_key(session):
    # DOCKER_HOST as a container env var would leak the control daemon endpoint — reject it.
    _enable_docker(session)
    with pytest.raises(ValueError):
        service.create_server(
            session, name="d", runner="docker", command="img:1", args=[],
            env={"DOCKER_HOST": "tcp://evil:2375"}, enabled=False,
        )


def test_docker_rejects_any_docker_cli_env_key(session):
    # Not just the allowlist — every DOCKER_* CLI var (API version, platform, custom headers)
    # is reserved: it would alter/break the control-plane's docker request.
    _enable_docker(session)
    for bad in ("DOCKER_API_VERSION", "DOCKER_DEFAULT_PLATFORM", "DOCKER_CUSTOM_HEADERS"):
        with pytest.raises(ValueError):
            service.create_server(
                session, name="d", runner="docker", command="img:1", args=[],
                env={bad: "x"}, enabled=False,
            )


def test_normalize_docker_warns_on_dropped_entrypoint():
    _, _, _, warnings = service.normalize_docker(
        "docker", ["run", "--entrypoint", "/srv", "img"], {}
    )
    assert any("entrypoint" in w for w in warnings)


def test_normalize_docker_servers_migrates_legacy_rows(session):
    # Simulate a legacy row (stored verbatim by the old runner-that-raised): command="docker".
    _enable_docker(session)
    s = service.create_server(
        session, name="legacy", runner="docker", command="img:1", args=[], env={}, enabled=False
    )
    # Force the row back into the legacy (non-canonical) shape directly via the repo.
    row = repo.get_server(session, s.id)
    row.command, row.args = "docker", ["run", "--rm", "-e", "T", "ghcr.io/x/y"]
    row.env = {"T": "v"}
    repo.save_server(session, row)

    changed = service.normalize_docker_servers(session)
    assert changed == 1
    migrated = repo.get_server(session, s.id)
    assert migrated.command == "ghcr.io/x/y"
    assert migrated.args == []
    assert migrated.env == {"T": "v"}
    # Idempotent: a second pass makes no further change.
    assert service.normalize_docker_servers(session) == 0


def test_normalize_docker_does_not_classify_podman():
    # `podman …` must NOT be parsed as a docker invocation (the runner always execs docker).
    image, args, env, _ = service.normalize_docker("podman", ["run", "--rm", "img"], {})
    assert image == "podman"  # treated as an image ref, not a launcher — i.e. not docker-parsed


def test_docker_update_clears_stale_cwd(session):
    _enable_docker(session)
    s = service.create_server(
        session, name="c", runner="command", command="/bin/x", args=[], cwd="/tmp"
    )
    assert s.cwd == "/tmp"
    u = service.update_server(session, s.id, {"runner": "docker", "command": "img:1", "args": []})
    assert u.runner == "docker" and u.cwd is None


def test_normalize_docker_bare_image_ref_passthrough():
    image, args, env, _ = service.normalize_docker("myrepo/img", ["--verbose"], {"K": "v"})
    assert image == "myrepo/img" and args == ["--verbose"] and env == {"K": "v"}


def test_normalize_docker_requires_image():
    with pytest.raises(ValueError):
        service.normalize_docker("docker", ["run", "-i", "--rm"], {})


def test_docker_create_disabled_allowed_and_stored_canonical(session):
    # Creating a DISABLED docker server is always allowed (review-before-enable) and stores
    # the canonical (image, container_args, env) shape even from a full invocation.
    s = service.create_server(
        session, name="gh", runner="docker",
        command="docker", args=["run", "--rm", "-e", "T", "img:1"], env={"T": "v"},
        enabled=False,
    )
    assert s.command == "img:1" and s.args == [] and s.env == {"T": "v"}


def test_docker_enable_gated_when_setting_off(session):
    s = service.create_server(
        session, name="gh", runner="docker", command="img:1", args=[], env={}, enabled=False
    )
    with pytest.raises(ValueError):
        service.set_enabled(session, s.id, True)


def test_docker_create_enabled_gated_when_setting_off(session):
    with pytest.raises(ValueError):
        service.create_server(
            session, name="gh", runner="docker", command="img:1", args=[], env={}, enabled=True
        )


def test_docker_enable_allowed_when_setting_on(session):
    _enable_docker(session)
    s = service.create_server(
        session, name="gh", runner="docker", command="img:1", args=[], env={}, enabled=False
    )
    enabled = service.set_enabled(session, s.id, True)
    assert enabled.enabled is True


def test_normalize_docker_warns_on_dropped_workdir_network_platform():
    # Codex: the hardened runner owns the invocation and drops these host-side run flags. Each
    # silently changes intended behavior, so importing must surface a review warning (both the
    # separated `--flag value` and the inline `--flag=value` spellings).
    for flag, needle in (("-w", "workdir"), ("--workdir", "workdir"),
                         ("--network", "network"), ("--net", "network"),
                         ("--platform", "platform"),
                         ("-u", "user"), ("--user", "user")):
        _, _, _, warnings = service.normalize_docker(
            "docker", ["run", flag, "val", "img"], {}
        )
        assert any(needle in w for w in warnings), (flag, warnings)
    # inline form
    _, _, _, w2 = service.normalize_docker("docker", ["run", "--network=none", "img"], {})
    assert any("network" in w for w in w2)


def test_normalize_docker_warns_on_dropped_daemon_selection():
    # Codex: a daemon/context selector before `run` (--context/-H/--host) is dropped — the runner
    # always targets mcpelevator's own daemon — so enabling would run on a different daemon than
    # the pasted config chose. Warn (both separated and inline spellings).
    for pre in (["--context", "prod"], ["-H", "tcp://daemon:2375"], ["--host", "tcp://d"]):
        _, _, _, warnings = service.normalize_docker("docker", [*pre, "run", "img"], {})
        assert any("daemon" in w for w in warnings), (pre, warnings)
    _, _, _, w2 = service.normalize_docker("docker", ["--context=prod", "run", "img"], {})
    assert any("daemon" in w for w in w2)
    # A normal invocation with no daemon selector must NOT warn about daemons.
    _, _, _, w3 = service.normalize_docker("docker", ["run", "--rm", "img"], {})
    assert not any("daemon" in w for w in w3)


def test_normalize_docker_warns_on_dropped_read_only():
    # --read-only is a BOOLEAN flag (no value) taken in the boolean-skip path; dropping it
    # silently weakens a config that hardened the rootfs, so it must still warn.
    _, _, _, warnings = service.normalize_docker("docker", ["run", "--read-only", "img"], {})
    assert any("read-only" in w for w in warnings)


def test_update_enabled_conversion_to_docker_is_gated(session):
    # Codex: converting an already-ENABLED non-docker server to docker while the runner is off
    # must be refused — PATCH can't disable the row, so it would otherwise start unreviewed the
    # moment the global docker_runner setting is toggled on.
    s = service.create_server(
        session, name="conv", runner="command", command="echo", args=["hi"], enabled=True,
    )
    with pytest.raises(ValueError):
        service.update_server(session, s.id, {"runner": "docker", "command": "img:1", "args": []})
    # With the runner enabled, the same conversion is allowed.
    _enable_docker(session)
    updated = service.update_server(
        session, s.id, {"runner": "docker", "command": "img:1", "args": []}
    )
    assert updated.runner == "docker" and updated.enabled is True


def test_update_disabled_conversion_to_docker_not_gated(session):
    # A DISABLED server can be converted to docker while the runner is off (reviewable); the gate
    # bites on enable, not on the edit.
    s = service.create_server(
        session, name="conv2", runner="command", command="echo", args=["hi"], enabled=False,
    )
    updated = service.update_server(session, s.id, {"runner": "docker", "command": "img:1"})
    assert updated.runner == "docker" and updated.enabled is False


def test_update_already_docker_enabled_edit_not_gated(session):
    # Editing a row that is ALREADY docker + enabled stays ungated even while the runner is off
    # (fix a broken image/env offline) — only a non-docker -> docker conversion is gated.
    from app.registry import settings as runtime_settings

    _enable_docker(session)
    s = service.create_server(
        session, name="dk", runner="docker", command="img:1", args=[], enabled=False,
    )
    service.set_enabled(session, s.id, True)
    runtime_settings.write(session, {"docker_runner": False})  # turn the runner off
    updated = service.update_server(session, s.id, {"command": "img:2"})
    assert updated.command == "img:2" and updated.enabled is True


def test_is_docker_launcher_distinguishes_launcher_from_image():
    # Codex: only a GENUINE docker CLI invocation (bare name or a filesystem path to it) is a
    # launcher. An OCI image ref whose final path segment is literally "docker" merely shares
    # the basename and must NOT be treated as the CLI.
    ok = service._is_docker_launcher
    assert ok("docker")
    assert ok("docker.exe")
    assert ok("/usr/local/bin/docker")
    assert ok("./docker")
    assert ok("~/bin/docker")
    assert ok(r"C:\Program Files\Docker\docker.exe")
    assert not ok("ghcr.io/acme/docker")
    assert not ok("docker.io/library/docker")
    assert not ok("npx")


def test_normalize_docker_image_named_docker_with_run_arg_not_misparsed():
    # Codex: an image ref whose basename is "docker" and whose OWN first arg happens to be
    # "run" must NOT be parsed as a `docker run` launcher — that would drop the real image and
    # mistake "run"'s next token for the image. A registry ref is preserved verbatim.
    img, args, _, _ = service.normalize_docker("ghcr.io/acme/docker", ["run", "serve"], {})
    assert img == "ghcr.io/acme/docker" and args == ["run", "serve"]
    img2, args2, _, _ = service.normalize_docker("docker.io/library/docker", ["run"], {})
    assert img2 == "docker.io/library/docker" and args2 == ["run"]


def test_command_runner_with_image_named_docker_not_reclassified(session):
    # A `command` runner whose command is an IMAGE ref ending in /docker (not the CLI) must NOT
    # be reclassified to the docker runner — only a genuine launcher is reclassified.
    s = service.create_server(
        session, name="img", runner="command", command="ghcr.io/acme/docker",
        args=["run"], env={}, enabled=False,
    )
    assert s.runner == "command" and s.command == "ghcr.io/acme/docker"


def test_normalize_docker_servers_migrates_npx_and_uvx_docker_rows(session):
    # Codex: an upgraded ENABLED row stored under runner="npx"/"uvx" with its command pointed
    # at the docker CLI must be canonicalized to the docker runner — otherwise reconcile's
    # `sv.runner == "docker"` gate never fires and it launches ungated with the full control
    # plane env even while the docker_runner setting is off.
    _enable_docker(session)
    ids = []
    for rn in ("npx", "uvx"):
        s = service.create_server(
            session, name=f"leg{rn}", runner="command", command="img:1", args=[], env={},
        )
        # Force the legacy passthrough shape directly via the repo (create would reclassify).
        row = repo.get_server(session, s.id)
        row.runner = rn
        row.command, row.args = "/usr/bin/docker", ["run", "--rm", "ghcr.io/x/y"]
        row.env = {}
        repo.save_server(session, row)
        ids.append(s.id)

    assert service.normalize_docker_servers(session) == 2
    for sid in ids:
        m = repo.get_server(session, sid)
        assert m.runner == "docker" and m.command == "ghcr.io/x/y"


def test_auth_provider_change_does_not_restart(session):
    """auth_provider is proxy-layer; changing it must NOT change config_hash
    (otherwise the reconciler would needlessly bounce the bridge)."""
    srv = _mk(session, args=["-y", "x"])
    before = srv.config_hash
    service.update_server(session, srv.id, {"auth_provider": "bearer"})
    assert repo.get_server(session, srv.id).config_hash == before
    # but a launch-affecting change still does
    service.update_server(session, srv.id, {"args": ["-y", "z"]})
    assert repo.get_server(session, srv.id).config_hash != before
