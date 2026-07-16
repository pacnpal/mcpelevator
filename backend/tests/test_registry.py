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


def test_concurrent_creates_get_unique_slugs(tmp_path):
    """Slug allocation is check-then-insert; API handlers now run registry writes in
    the threadpool, so without the service write lock two same-name creates could both
    pick the base slug and one would die on the unique constraint instead of getting
    the -2 suffix. File-backed DB so every thread's connection sees the same data."""
    from concurrent.futures import ThreadPoolExecutor

    from app.db import models  # noqa: F401 — register tables

    engine = create_engine(
        f"sqlite:///{tmp_path}/reg.db", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)

    def make(_):
        with Session(engine) as s:
            return service.create_server(s, name="Same Name", runner="npx", command="npx").slug

    with ThreadPoolExecutor(max_workers=4) as ex:
        slugs = sorted(ex.map(make, range(4)))
    assert slugs == ["same-name", "same-name-2", "same-name-3", "same-name-4"]


def test_update_through_stale_session_hashes_the_fresh_row(tmp_path):
    """The PATCH handler pre-reads the row into its request session before the locked
    update runs; if another request commits in between, the update must re-read the row
    (not trust its identity map) or it stores a config_hash describing a stale snapshot."""
    from app.db import models  # noqa: F401 — register tables

    engine = create_engine(
        f"sqlite:///{tmp_path}/reg.db", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s1, Session(engine) as s2:
        sid = service.create_server(s1, name="x", runner="npx", command="npx").id
        # Request B primes its session, as the API handler does. The binding matters:
        # the handler holds this object in a local across its await, and SQLAlchemy's
        # identity map is weak — an unreferenced row would be GC'd and re-fetched fresh,
        # hiding the staleness this test exists to catch.
        primed = repo.get_server(s2, sid)
        service.update_server(s1, sid, {"args": ["-y", "pkg"]})  # request A lands first
        # B edits a different hash-bearing field: without the locked re-read, B would
        # merge onto its stale snapshot and store a hash over (old args, new env).
        service.update_server(s2, sid, {"env": {"K": "v"}})
        del primed
    with Session(engine) as s3:
        row = repo.get_server(s3, sid)
        assert row.args == ["-y", "pkg"]  # A's edit survived B's disjoint PATCH
        assert row.env == {"K": "v"}
        assert row.config_hash == service.compute_hash(row)  # hash describes the final row


def test_reserved_slug_is_not_assigned(session):
    """A server named "summary" must not get the slug "summary" — that would shadow
    the static /api/health/summary route so its own /api/health/{slug} is unreachable.
    It's disambiguated instead."""
    a = _mk(session, name="summary")
    assert a.slug == "summary-2"
    # the reserved word stays free no matter how the name is cased/spaced
    b = _mk(session, name="Summary")
    assert b.slug == "summary-3"


def test_all_slug_is_allowed(session):
    """"all" is NOT reserved: group endpoints live under /g/<name>, so a server may be
    slugged "all" and served at /s/all without any collision."""
    a = _mk(session, name="all")
    assert a.slug == "all"


def test_config_hash_changes_on_edit(session):
    a = _mk(session, args=["-y", "x"])
    before = a.config_hash
    service.update_server(session, a.id, {"args": ["-y", "z"]})
    after = repo.get_server(session, a.id).config_hash
    assert after != before


def test_setup_script_round_trips_hashes_and_clones(session):
    script = "printf 'installing\\n'\nmkdir -p .cache/setup\n"
    server = _mk(session, setup_script=script)
    assert server.setup_script == script

    before = server.config_hash
    updated = service.update_server(session, server.id, {"setup_script": "printf 'updated\\n'\n"})
    assert updated.setup_script == "printf 'updated\\n'\n"
    assert updated.config_hash != before

    clone = service.clone_server(session, server.id)
    assert clone.setup_script == updated.setup_script
    assert clone.config_hash == updated.config_hash


def test_setup_script_blank_is_canonical_and_local_only(session):
    blank = _mk(session, setup_script="  \n\t")
    assert blank.setup_script == ""

    with pytest.raises(ValueError, match="Docker image"):
        service.create_server(
            session,
            name="Docker",
            runner="docker",
            command="img:1",
            setup_script="echo no",
        )
    with pytest.raises(ValueError, match="local runners"):
        service.create_server(
            session,
            name="Remote",
            runner="remote",
            command="https://up.example/mcp",
            setup_script="echo no",
        )


def test_setup_script_cannot_bypass_docker_runner_reclassification(session):
    with pytest.raises(ValueError, match="Docker image"):
        service.create_server(
            session,
            name="Docker",
            runner="command",
            command="docker",
            args=["run", "img:1"],
            setup_script="echo no",
        )


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


_DOCKER_GUARD_MSG = "Docker CLI invocations require the docker runner"


def test_shell_wrapped_docker_create_enabled_rejected(session):
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session,
            name="wrapped",
            runner="command",
            command="/bin/sh",
            args=["-c", "docker run --privileged -v /:/host alpine sh"],
            enabled=True,
        )


def test_shell_wrapped_docker_enable_rejected(session):
    s = service.create_server(
        session,
        name="wrapped",
        runner="command",
        command="/bin/sh",
        args=["-c", "docker run --privileged -v /:/host alpine sh"],
        enabled=False,
    )
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.set_enabled(session, s.id, True)


def test_shell_wrapped_docker_update_enabled_rejected(session):
    s = service.create_server(
        session, name="plain", runner="command", command="echo", args=["hi"], enabled=True
    )
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.update_server(
            session, s.id, {"command": "/bin/bash", "args": ["-lc", "docker run alpine"]}
        )
    reloaded = repo.get_server(session, s.id)
    assert reloaded.command == "echo"


@pytest.mark.parametrize(
    ("command", "args"),
    [
        # "docker" as a plain argument to a non-docker command, not a command-position launcher.
        ("/bin/sh", ["-c", "python -m mcp_server --backend docker"]),
        # A long option that merely contains 'c' is not -c, so nothing is scanned as a command.
        ("/bin/bash", ["--norc", "--", "python", "-m", "srv"]),
    ],
)
def test_shell_wrapped_docker_argument_allowed(session, command, args):
    """The guard must not fire on configs that only mention docker as an argument, or use a
    long shell option that happens to contain the letter 'c'."""
    s = service.create_server(
        session, name="arg_allowed", runner="command", command=command, args=args, enabled=True
    )
    assert s.enabled is True


def test_shell_wrapped_docker_via_env_wrapper_rejected(session):
    """``env`` (optionally with assignments) fronting a shell that launches docker must still be
    caught — the env basename would otherwise mask the wrapped invocation."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session,
            name="env-wrapped",
            runner="command",
            command="/usr/bin/env",
            args=["FOO=bar", "bash", "-c", "docker run alpine"],
            enabled=True,
        )


def test_shell_wrapped_docker_via_norc_before_c_rejected(session):
    """A valid long option before the real ``-c`` must not short-circuit detection."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session,
            name="norc",
            runner="command",
            command="/bin/bash",
            args=["--norc", "-c", "docker run alpine"],
            enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        "sudo docker run alpine",
        "sudo -u root docker run alpine",  # -u consumes its value; docker is still the command
        "sudo -n docker run alpine",       # sudo -n (--non-interactive) is a boolean, not a value
        "env -u PATH docker run alpine",
        "nice -n 10 docker run alpine",    # nice -n IS value-taking
        "exec docker run alpine",
        "foo && docker run alpine",        # docker in a command position after a shell operator
        "true&&docker run alpine",         # ...even glued to the operator with no spaces
        "echo x;docker run alpine",
        "foo | docker run alpine",
        "$(docker run alpine)",            # command substitution
        "env -S 'docker run alpine'",      # env -S split-string bearing the command
        "env -S 'bash -c \"docker run alpine\"'",
        "systemd-run --scope docker run alpine",   # systemd-run runs COMMAND in a transient unit
        "systemd-run -p MemoryMax=1G docker run",  # -p consumes its property value; docker follows
        "script -q -c 'docker run alpine' /dev/null",  # util-linux script -c runs COMMAND via sh -c
    ],
)
def test_shell_wrapped_docker_via_wrapper_options_rejected(session, inner):
    """Thin wrappers (sudo/env/nice/…), glued operators, command substitution, and env -S must not
    slip the guard."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session,
            name="wrapped-opt",
            runner="command",
            command="/bin/sh",
            args=["-c", inner],
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("sudo", ["docker", "run", "alpine"]),          # a bare thin wrapper as the command
        ("nice", ["-n", "10", "docker", "run"]),        # ...with a value-taking option
        ("sudo", ["FOO=bar", "docker", "run"]),         # sudo's leading VAR=value assignment
        ("/usr/bin/env", ["-S", "docker run alpine"]),  # env -S at the top level
        ("/usr/bin/env", ["-vS", "docker run alpine"]),  # ...clustered with a boolean short opt
        ("/usr/bin/env", ["bash", "-c", "docker run"]),
        ("systemd-run", ["--scope", "docker", "run", "alpine"]),  # transient-unit wrapper
        ("systemd-run", ["-M", "container", "docker", "run"]),    # -M consumes its machine value
        ("script", ["-q", "-c", "docker run alpine", "/dev/null"]),  # script -c COMMAND typescript
        ("runuser", ["-u", "root", "docker", "run", "alpine"]),   # runuser -u USER direct-exec form
        ("runuser", ["--user", "root", "docker", "run"]),         # ...long form gives the user too
    ],
)
def test_top_level_wrapper_docker_rejected(session, command, args):
    """A docker CLI fronted by a thin wrapper as the stored command (not inside ``-c``) is still a
    docker launch and must be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="top-wrap", runner="command", command=command, args=args, enabled=True
        )


@pytest.mark.parametrize(
    "inner",
    [
        "FOO=bar docker run alpine",               # leading shell assignment before the command
        "if docker run alpine; then true; fi",     # reserved word before the command
        "time docker run alpine",                  # `time` keyword
        "! docker run alpine",                     # `!` negation keyword
        "while docker run x; do :; done",
        "exec -a ignored docker run alpine",       # exec -a consumes its value
        "sudo -Eu root docker run alpine",         # clustered short opts with embedded value opt
        "sudo FOO=bar docker run alpine",          # sudo assignment inside -c
        'echo "$(docker run alpine)"',             # command substitution inside double quotes
        "echo `docker run alpine`",                # backtick substitution
        'eval "docker run alpine"',                # eval re-parses its operand as a command line
        "eval docker run alpine",                  # ...even unquoted, joined back into one line
        "trap 'docker run alpine' EXIT",           # trap runs its action on the EXIT pseudo-signal
        "trap 'docker run alpine' 0",              # ...POSIX spells the exit trap as signal 0
        "doc\\\nker run alpine",                    # line continuation folds ``doc\<newline>ker``
        "docker run \\\nalpine",                    # ...continuation between the command and its args
    ],
)
def test_shell_wrapped_docker_control_syntax_rejected(session, inner):
    """Assignments, reserved words, exec -a, clustered options, command substitution, and eval must
    not let a docker launch slip past the guard."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session,
            name="ctrl-syntax",
            runner="command",
            command="/bin/sh",
            args=["-c", inner],
            enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        "FOO=docker myapp run",              # 'docker' is an assignment VALUE, not the command
        "echo 'literal $(docker) text'",     # substitution inside single quotes is literal
        "myapp --label docker.io/img",       # 'docker' only in an argument
        "env MYVAR=1 python app.py",         # env assignment fronting a non-docker command
    ],
)
def test_shell_wrapped_docker_control_syntax_allowed(session, inner):
    """Control syntax that only *mentions* docker (assignment value, single-quoted, an argument)
    must not trigger a false rejection."""
    s = service.create_server(
        session, name="ctrl-ok", runner="command", command="/bin/sh", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    "inner",
    [
        # A quoted ')' inside the $(...) body must not end the substitution early.
        "echo \"$(printf ')' ; docker run alpine)\"",
        "docker\\\n run alpine",               # a line continuation the shell removes
    ],
)
def test_shell_wrapped_docker_quoting_and_continuation_rejected(session, inner):
    """Quote-aware substitution balancing, line continuations, and ANSI-C quoting must not let a
    docker launch slip past the guard."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="quote-cont", runner="command", command="/bin/sh",
            args=["-c", inner], enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("/usr/bin/env", ["-S", "docker\\_run alpine"]),   # GNU env -S '\_' is a separator
        ("/usr/bin/env", ["-vS", "docker\\_run alpine"]),  # ...clustered with a boolean short opt
        # Pathological wrapper nesting beyond the peel bound resolves conservatively to docker.
        ("/bin/sh", ["-c", "env " * 70 + "docker run alpine"]),
    ],
)
def test_env_split_and_deep_nesting_rejected(session, command, args):
    """env -S split-string escapes and pathological wrapper nesting must still be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="env-s-deep", runner="command", command=command, args=args, enabled=True
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        # env -S split value re-enters env's grammar: a leading -- or NAME=VALUE still execs docker.
        ("/usr/bin/env", ["-S", "-- docker run alpine"]),
        ("/usr/bin/env", ["-S", "FOO=bar docker run alpine"]),
    ],
)
def test_env_split_string_grammar_rejected(session, command, args):
    """A ``--`` or assignment at the start of an ``env -S`` split string must not hide the docker
    command behind it."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="env-s-grammar", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        # `docker` here is a script argument / comment / loop variable / arithmetic operand — never
        # a command the shell executes, so none of these should be rejected.
        ("/bin/bash", ["script.sh", "-c", "docker run alpine"]),   # script operand, not a -c string
        ("/bin/sh", ["-c", "for docker in 1; do echo \"$docker\"; done"]),  # loop variable name
    ],
)
def test_shell_docker_non_command_positions_allowed(session, command, args):
    """A shell script operand, comment text, loop variable, or arithmetic operand named ``docker``
    must not trigger a false rejection."""
    s = service.create_server(
        session, name="non-cmd", runner="command", command=command, args=args, enabled=True
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("/bin/bash", ["-o", "nounset", "-c", "docker run alpine"]),   # -o value before -c
        ("/bin/bash", ["-O", "extglob", "-c", "docker run alpine"]),   # -O value before -c
        ("timeout", ["30", "docker", "run", "alpine"]),                # timeout DURATION COMMAND
        ("timeout", ["-s", "KILL", "30", "docker", "run"]),            # ...with a value option
        ("/bin/sh", ["-c", "timeout 5 docker run alpine"]),            # timeout inside a -c string
        # A command substitution INSIDE arithmetic is still executed by the shell.
        ("/bin/bash", ["-c", "echo $(( $(docker run alpine) + 1))"]),
    ],
)
def test_option_operands_and_timeout_and_arith_subst_rejected(session, command, args):
    """Operand-taking shell options, the ``timeout`` wrapper, and command substitution nested in
    arithmetic must all still be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="opt-timeout", runner="command", command=command, args=args, enabled=True
        )


def test_timeout_non_docker_command_allowed(session):
    """``timeout`` fronting a non-docker command must not be gated."""
    s = service.create_server(
        session, name="timeout-ok", runner="command", command="timeout",
        args=["30", "python", "app.py"], enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("timeout", ["--", "30", "docker", "run", "alpine"]),   # -- ends opts; DURATION still first
    ],
)
def test_timeout_dashdash_and_brace_expansion_rejected(session, command, args):
    """``timeout --`` (duration after end-of-options) and brace-expanded launchers must be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="dd-brace", runner="command", command=command, args=args, enabled=True
        )


@pytest.mark.parametrize(
    "inner",
    [
        "command -v docker >/dev/null && exec server",  # `command -v` only LOOKS UP docker
        "command -V docker",
        "myapp --opt={a,docker}",                       # brace in an argument, not the command word
    ],
)
def test_command_lookup_and_arg_brace_allowed(session, inner):
    """`command -v/-V` (a lookup, not a launch) and brace expansion in an argument must not be
    mistaken for a docker launch."""
    s = service.create_server(
        session, name="lookup-ok", runner="command", command="/bin/sh", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    "inner",
    [
        "time -p docker run alpine",           # `time -p` options before the pipeline
        "<(docker run alpine) cat",            # process substitution
        "case x in *) docker run;; esac",      # docker in a case BODY (a real command)
        "for x in a; do docker run; done",     # docker in a loop BODY
    ],
)
def test_shell_keywords_and_quoting_rejected(session, inner):
    """trap/coproc/time options, glued redirections, process substitution, ANSI-C escapes, and
    docker inside a case/loop body must all be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="kw", runner="command", command="/bin/sh", args=["-c", inner],
            enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        "case docker in docker) echo ok;; esac",   # `docker` is the case SUBJECT/pattern, not a cmd
        "for docker in 1 2 3; do echo hi; done",    # `docker` is the loop variable
        "for x in docker podman; do echo hi; done",  # `docker` is a list item
    ],
)
def test_case_subject_and_loop_list_allowed(session, inner):
    """A `case` subject/pattern or `for` list item named ``docker`` is data, not a launched command,
    and must not be rejected."""
    s = service.create_server(
        session, name="kw-ok", runner="command", command="/bin/sh", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    "inner",
    [
        "printf x | xargs docker run alpine",       # xargs runs its COMMAND
        "printf x | xargs -n1 -P4 docker run",       # ...with value options
        "printf x | xargs -I{} docker run {}",
        "cat <<EOF && docker run alpine\nhi\nEOF",   # docker on the heredoc MARKER line runs
        "[[ -n $X ]] && docker run alpine",          # docker after ]] is a real command
    ],
)
def test_xargs_heredoc_marker_and_conditional_rejected(session, inner):
    """xargs child commands, a docker command on a heredoc marker line, and a real command after a
    ``[[ … ]]`` test must all be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="xargs-hd", runner="command", command="/bin/sh", args=["-c", inner],
            enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        "printf x | xargs echo docker",               # docker is an xargs INITIAL-ARG, not the cmd
    ],
)
def test_heredoc_body_and_conditional_operand_allowed(session, inner):
    """A docker string in a heredoc body, a `[[ … ]]` operand, or an xargs argument is data, not a
    launched command, and must not be rejected."""
    s = service.create_server(
        session, name="hd-ok", runner="command", command="/bin/sh", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("stdbuf", ["-oL", "docker", "run", "alpine"]),        # stdbuf launches its COMMAND
        ("xargs", ["-E", "STOP", "docker", "run", "alpine"]),  # xargs -E EOF takes a value
    ],
)
def test_stdbuf_and_xargs_eof_rejected(session, command, args):
    """stdbuf (a command-launching wrapper) and xargs' value-taking ``-E`` must be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="stdbuf-x", runner="command", command=command, args=args, enabled=True
        )


@pytest.mark.parametrize(
    "inner",
    [
        "[[ $(docker run alpine) == x ]]",   # substitution inside a [[ … ]] condition executes
        "cat <<EOF\n$(docker run alpine)\nEOF",  # unquoted heredoc expands its body's substitution
    ],
)
def test_substitutions_in_conditional_and_heredoc_rejected(session, inner):
    """A command substitution inside a ``[[ … ]]`` condition or an unquoted here-document body still
    runs, so it must be inspected even though the surrounding operands/data are dropped."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="sub-cond", runner="command", command="/bin/sh", args=["-c", inner],
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        ("flock", ["/tmp/lock", "docker", "run", "alpine"], None),   # flock FILE COMMAND
        ("flock", ["-w", "5", "/tmp/lock", "docker", "run"], None),  # ...with a value option
        ("flock", ["/tmp/lock", "-c", "docker run alpine"], None),   # flock FILE -c SHELL-COMMAND
        (None, None, "builtin exec docker run alpine"),
        (None, None, ">/dev/null docker run alpine"),                # a leading redirection
        (None, None, "2>/dev/null docker run alpine"),
        (None, None, "echo ok # <<'EOF'\n$(docker run alpine)\nEOF"),  # heredoc marker in a comment
    ],
)
def test_flock_builtin_redirection_and_comment_heredoc_rejected(session, command, args, inner):
    """flock/builtin wrappers, leading redirections, and a here-doc marker that is only a comment
    (so the next line still runs) must all be gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="flock-b", runner="command", command=command, args=args, enabled=True
        )


def test_flock_non_docker_command_allowed(session):
    """flock fronting a non-docker command must not be gated."""
    s = service.create_server(
        session, name="flock-ok", runner="command", command="flock",
        args=["/tmp/lock", "python", "app.py"], enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        (None, None, "command -p /usr/bin/docker run alpine"),            # `command -p` runs it
        (None, None, 'echo function docker; docker run alpine'),          # not a real func decl
        (None, None, '[[ "]]" == x ]] ; docker run alpine'),             # quoted ]] isn't the end
    ],
)
def test_find_exec_command_opts_and_quoted_conditional_rejected(session, command, args, inner):
    """find's -exec child, `command`'s own options, a `function` token that is only an argument, and
    a quoted ``]]`` inside a conditional must not let a real docker launch slip through."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="find-x", runner="command", command=command, args=args, enabled=True
        )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("find", [".", "-exec", "rm", "{}", ";"]),   # find -exec of a non-docker command
        ("find", [".", "-name", "*.docker"]),         # 'docker' only in a -name pattern
    ],
)
def test_find_non_docker_allowed(session, command, args):
    """A find whose -exec child isn't docker (or that merely matches a docker-ish name) is allowed."""
    s = service.create_server(
        session, name="find-ok", runner="command", command=command, args=args, enabled=True
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    "inner",
    [
        "docker run alpine; function docker { :; }",  # real launch BEFORE a later shadow decl
        "bash <<EOF\ndocker run alpine\nEOF",         # heredoc body fed to a shell IS a script
        "bash <<'EOF'\ndocker run alpine\nEOF",       # ...even with a quoted delimiter
    ],
)
def test_brace_truncation_function_order_and_shell_stdin_rejected(session, inner):
    """A brace expansion that truncates (fail closed), a docker launch preceding a later shadowing
    ``function`` declaration, and heredoc/here-string bodies fed to a shell must all be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="brace-fn-stdin", runner="command", command="/bin/sh",
            args=["-c", inner], enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        "cat <<< docker",                              # here-string fed to cat is data
    ],
)
def test_function_shadow_and_non_shell_stdin_allowed(session, inner):
    """A call to a previously-defined ``docker`` function, and heredoc/here-string bodies fed to a
    NON-shell (cat), are not docker launches and must not be rejected."""
    s = service.create_server(
        session, name="fn-stdin-ok", runner="command", command="/bin/sh", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("/usr/bin/rbash", ["-c", "docker run alpine"]),      # restricted bash still honors -c
        ("chroot", ["/", "docker", "run", "alpine"]),         # chroot NEWROOT COMMAND
        ("chroot", ["--userspec", "x", "/", "docker", "run"]),
    ],
)
def test_rbash_and_chroot_rejected(session, command, args):
    """rbash's -c command string and chroot's child command must be gated."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="rbash-chroot", runner="command", command=command, args=args,
            enabled=True,
        )


def test_positionals_without_param_reference_allowed(session):
    """Positional args that the `-c` script never references are inert and must not be rejected."""
    s = service.create_server(
        session, name="pos-ok", runner="command", command="/bin/sh",
        args=["-c", "echo hi", "x", "docker", "run"], enabled=True,
    )
    assert s.enabled is True


def test_setup_script_invoking_docker_rejected(session):
    """A setup script runs as ``/bin/sh -e -c`` with the passthrough env, so a docker invocation in
    it must be gated on create, update, and enable — not just in command/args."""
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="ss1", runner="command", command="echo", args=["hi"],
            setup_script="docker run alpine", enabled=True,
        )
    # create disabled, then enable → still gated
    s = service.create_server(
        session, name="ss2", runner="command", command="echo", args=["hi"],
        setup_script="docker run alpine", enabled=False,
    )
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.set_enabled(session, s.id, True)
    # update an enabled server's setup_script to a docker invocation → gated
    s2 = service.create_server(
        session, name="ss3", runner="command", command="echo", args=["hi"], enabled=True,
    )
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.update_server(session, s2.id, {"setup_script": "docker run alpine"})


def test_setup_script_without_docker_allowed(session):
    s = service.create_server(
        session, name="ss-ok", runner="command", command="echo", args=["hi"],
        setup_script="pip install -r requirements.txt", enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        (None, None, "bin/docker run alpine"),          # relative path executable
        (None, None, "./docker run alpine"),
        ("runuser", ["-u", "root", "--", "docker", "run"], None),  # runuser -u user -- COMMAND
        ("runuser", ["-u", "root", "-c", "docker run alpine"], None),  # runuser -c SHELL-COMMAND
        ("su", ["-c", "docker run alpine", "root"], None),         # su -c SHELL-COMMAND
    ],
)
def test_ifs_relative_path_and_runuser_rejected(session, command, args, inner):
    """$IFS field-splitting, a relative docker path, and runuser/su wrappers must be gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="ifs-ru", runner="command", command=command, args=args, enabled=True
        )


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "printf '%s' '$@'", "ignored", "docker", "run"],   # $@ is single-quoted -> literal
        ["-c", "bin/python app.py"],                              # relative path, not docker
    ],
)
def test_quoted_positional_and_relative_nondocker_allowed(session, args):
    """A single-quoted ``$@`` (literal) and a relative non-docker path must not be rejected."""
    s = service.create_server(
        session, name="q-pos-ok", runner="command", command="/bin/sh", args=args, enabled=True
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        ("watch", ["docker", "run", "alpine"], None),           # watch COMMAND (via sh -c)
        ("watch", ["-n", "5", "docker", "run"], None),          # ...with a value option
        (None, None, "FOO=bar sh <<'EOF'\ndocker run alpine\nEOF"),  # heredoc + leading assignment
    ],
)
def test_watch_heredoc_assignment_and_param_default_rejected(session, command, args, inner):
    """The watch wrapper, a heredoc receiver behind a leading assignment, and a ``${VAR:-docker}``
    default expansion must all be gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="watch-pd", runner="command", command=command, args=args, enabled=True
        )


def test_param_default_non_docker_allowed(session):
    """A ``${VAR:-python}`` default that isn't docker must not be rejected."""
    s = service.create_server(
        session, name="pd-ok", runner="command", command="/bin/sh",
        args=["-c", "${VAR:-python} -m srv"], enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``command`` suppresses shell-function lookup, so a shadowing definition does NOT protect it.
        (None, None, "function docker { :; }; command docker run alpine"),
        (None, None, "docker() { :; }; command docker run alpine"),
        # ``watch`` runs its single operand string through ``sh -c`` by default.
        ("watch", ["docker run alpine"], None),
        # ...and passes the argv straight to exec with -x/--exec (first operand is the command).
        ("watch", ["-x", "docker", "run", "alpine"], None),
        ("watch", ["--exec", "docker", "run", "alpine"], None),
        # ``source``/``.`` pointed at stdin execute the heredoc body as script in the current shell.
        (None, None, "source /dev/stdin <<'EOF'\ndocker run alpine\nEOF"),
        (None, None, ". /dev/stdin <<'EOF'\ndocker run alpine\nEOF"),
        (None, None, "source - <<'EOF'\ndocker run alpine\nEOF"),
    ],
)
def test_command_builtin_watch_string_and_sourced_stdin_rejected(session, command, args, inner):
    """``command`` bypassing a function shadow, ``watch``'s ``sh -c`` operand string (and its
    ``-x``/``--exec`` argv form), and a heredoc executed via ``source``/``.`` from stdin must all be
    gated — each reaches the docker CLI without the docker runner's hardening."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="cmd-watch-src", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``ionice`` runs a supplied command like ``nice`` — its value options must not swallow it.
        ("ionice", ["-c", "3", "docker", "run", "alpine"], None),
        ("ionice", ["--class", "3", "docker", "run"], None),
        ("ionice", ["-c3", "docker", "run"], None),          # value glued into the short cluster
        ("ionice", ["-t", "docker", "run"], None),           # -t is boolean (no value)
        # ``sudo -s``/``-i`` pass the trailing operands to a shell via ``-c`` — a command LINE.
        ("sudo", ["-s", "true; docker run alpine"], None),
        ("sudo", ["-i", "true; docker run alpine"], None),
        ("sudo", ["--shell", "docker run"], None),
    ],
)
def test_ionice_sudo_shell_and_variable_command_word_rejected(session, command, args, inner):
    """``ionice`` as a command launcher, ``sudo -s``/``-i`` shell command lines, and a command word
    resolved from a prior literal assignment (``D=docker; "$D" run``) must all be gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="ionice-sudo-var", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        ("ionice", ["-c", "3", "python", "-m", "srv"], None),   # ionice wrapping a non-docker cmd
        ("sudo", ["-s", "python -m srv"], None),                # sudo -s with a non-docker command
        (None, None, "D=python; $D -m srv"),                    # assignment resolves to non-docker
        (None, None, "D=docker; '$D' run"),                     # single-quoted $D never expands
        (None, None, "D=docker echo hi; $D run"),               # env-scoped assign, not persisted
    ],
)
def test_ionice_sudo_shell_and_variable_non_docker_allowed(session, command, args, inner):
    """``ionice``/``sudo -s`` wrapping a non-docker command, and variable command words that do NOT
    resolve to docker (non-docker value, single-quoted, or env-scoped assignment), must not be
    rejected."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    s = service.create_server(
        session, name="ionice-sudo-var-ok", runner="command", command=command, args=args,
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``taskset MASK COMMAND`` (leading affinity mask) and ``taskset -c LIST COMMAND``.
        ("taskset", ["0xff", "docker", "run", "alpine"], None),
        ("taskset", ["-c", "0-3", "docker", "run"], None),
    ],
)
def test_taskset_npx_call_export_and_stdin_herestring_rejected(session, command, args, inner):
    """``taskset`` launchers (leading affinity mask or ``-c`` cpu-list) fronting docker must be
    gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="taskset-npx-src", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner", "env"),
    [
        ("taskset", ["0x1", "python", "-m", "srv"], None, None),   # taskset wrapping non-docker
        ("npx", ["-y", "@scope/pkg"], None, None),                 # ordinary npx (no -c)
        ("npx", ["-c", "echo hi"], None, None),                    # npx -c non-docker
        (None, None, "export D=python; $D -m srv", None),          # export non-docker
        (None, None, "${D} -m srv", {"D": "python"}),              # env-resolved non-docker
    ],
)
def test_taskset_npx_export_and_env_non_docker_allowed(session, command, args, inner, env):
    """The new launchers/resolvers wrapping a NON-docker command must not be rejected."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    s = service.create_server(
        session, name="new-launchers-ok", runner="command", command=command, args=args,
        env=env or {}, enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``unshare`` runs a supplied command after its namespace flags.
        ("unshare", ["docker", "run", "alpine"], None),
        ("unshare", ["-r", "docker", "run"], None),
        ("unshare", ["-m", "-p", "docker", "run"], None),
        ("unshare", ["-R", "/root", "docker", "run"], None),      # -R consumes a value
    ],
)
def test_unshare_pipe_shell_env_assignment_and_set_positionals_rejected(
        session, command, args, inner):
    """``unshare`` launchers, a static literal piped into a stdin-reading shell, an ``env NAME=VALUE``
    the child shell expands, and ``set --`` positionals executed via ``"$@"`` must all be gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="unshare-pipe-set", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        "echo hello | sh",                     # piped literal isn't docker
        "printf 'docker run' | grep x",        # piped into a NON-shell (grep) — just data
        'set -- python -m srv; "$@"',          # positionals resolve to a non-docker command
        "set -e; echo hi",                     # ``set`` options install no positionals
    ],
)
def test_unshare_pipe_and_set_non_docker_allowed(session, inner):
    """Static-literal pipes into a non-shell or a non-docker program, and ``set`` forms that don't
    install a docker positional, must not be rejected."""
    s = service.create_server(
        session, name="pipe-set-ok", runner="command", command="/bin/sh", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


def test_env_assignment_expanded_command_allowed_when_non_docker(session):
    """An ``env NAME=VALUE`` whose child shell expands it to a NON-docker command must not be
    rejected."""
    s = service.create_server(
        session, name="env-assign-ok", runner="command", command="env",
        args=["D=python", "sh", "-c", '"$D" -m srv'], enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # A literal ``docker`` command word inside a function-body segment (up to the ``;``) is caught.
        (None, None, "function f { docker run; }; f"),
        (None, None, "docker() { command docker run; }; docker"),  # command bypasses the shadow
        # ``prlimit`` runs its trailing command after resource limits.
        ("prlimit", ["--nofile=1024", "docker", "run", "alpine"], None),
        # Process substitution whose inner segment is a literal docker command.
        (None, None, "cat <(docker run alpine)"),
    ],
)
def test_function_body_prlimit_npm_alias_printf_and_procsub_rejected(
        session, command, args, inner):
    """A called function's docker body, ``prlimit``, ``npm`` global options before ``exec``, Bash
    aliases, ``printf`` operand substitution piped to a shell, and process substitution must all be
    gated."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="fn-prlimit-alias", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        (None, None, "f() { docker run alpine; }; echo hi"),  # function defined but never called
        ("prlimit", ["--nofile=1024", "python", "-m", "srv"], None),
        ("npm", ["exec", "somepkg"], None),                   # npm exec without -c/--call
        (None, None, "alias d=python; d -m srv"),             # alias resolves to non-docker
        (None, None, "bash <(printf 'echo hi\\n')"),          # process subst output isn't docker
    ],
)
def test_function_body_prlimit_npm_alias_and_procsub_non_docker_allowed(
        session, command, args, inner):
    """The body/wrapper/alias/procsub machinery must not reject the non-docker cases: an uncalled
    function, a benign shadow, and non-docker prlimit/npm/alias/process-substitution forms."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    s = service.create_server(
        session, name="fn-alias-ok", runner="command", command=command, args=args, enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    "inner",
    [
        "source <(printf 'echo hi\\n')",         # sourced process-sub output isn't docker
        "D=python sh -c '\"$D\" -m srv'",        # inline assignment resolves to non-docker
        "printf hi | cat /etc/hostname | sh",    # ``cat FILE`` reads the file, not the piped stdin
    ],
)
def test_sourced_procsub_inline_assignment_and_passthrough_non_docker_allowed(session, inner):
    """The sourced-procsub / inline-assignment / pass-through-pipe machinery must not reject the
    non-docker cases."""
    s = service.create_server(
        session, name="src-inline-ok", runner="command", command="/bin/bash", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``chrt [policy] PRIORITY COMMAND`` launches a command after its scheduling priority.
        ("chrt", ["-o", "0", "docker", "run", "alpine"], None),
        ("chrt", ["-f", "50", "docker", "run"], None),
    ],
)
def test_chrt_printf_b_and_param_transform_rejected(session, command, args, inner):
    """``chrt [policy] PRIORITY COMMAND`` fronting docker must be gated."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="chrt-printf-xform", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        ("chrt", ["-f", "50", "python", "-m", "srv"], None),  # chrt wrapping a non-docker command
        (None, None, "${FOO:0} python"),                      # transform on an unknown var → empty
        (None, None, "D=python; ${D:0} -m srv"),              # transform resolves to non-docker
        # Aliases are NOT expanded without ``shopt -s expand_aliases`` in a non-interactive shell,
        # so a benign alias definition must not be treated as a launch.
        (None, None, "alias d=docker; d run alpine"),
        (None, None, "alias d='docker run'; d alpine"),
    ],
)
def test_chrt_transform_and_unexpanded_alias_non_docker_allowed(session, command, args, inner):
    """Non-docker ``chrt``, unresolved/non-docker parameter transforms, and aliases without
    ``expand_aliases`` enabled must not be rejected."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    s = service.create_server(
        session, name="chrt-alias-ok", runner="command", command=command, args=args, enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``su``/``runuser`` take a USER positional before their ``-c`` shell command.
        ("su", ["root", "-c", "docker run alpine"], None),
        ("su", ["-l", "root", "-c", "docker run"], None),
        ("su", ["-", "root", "-c", "docker run"], None),
        ("runuser", ["-l", "root", "-c", "docker run"], None),
    ],
)
def test_substitution_and_body_assignments_and_su_user_operand_rejected(
        session, command, args, inner):
    """A prior assignment resolved inside a command substitution or function body, and ``su``/
    ``runuser`` with a user positional before ``-c``, must all be gated."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="subst-body-su", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        (None, None, 'D=python; echo "$($D -m srv)"'),       # substitution resolves to non-docker
        (None, None, 'D=python; f() { "$D" -m srv; }; f'),   # function body resolves to non-docker
        ("su", ["root", "-c", "python -m srv"], None),       # su -c a non-docker command
        ("su", ["root"], None),                              # su with no command (interactive)
    ],
)
def test_substitution_body_assignment_and_su_non_docker_allowed(session, command, args, inner):
    """The substitution/function-body variable resolution and ``su`` user-operand handling must not
    reject the non-docker cases."""
    if inner is not None:
        command, args = "/bin/sh", ["-c", inner]
    s = service.create_server(
        session, name="subst-su-ok", runner="command", command=command, args=args, enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``taskset -- MASK COMMAND``: ``--`` ends options but the affinity mask still precedes cmd.
        ("taskset", ["--", "0xff", "docker", "run", "alpine"], None),
        ("taskset", ["-c", "0-3", "--", "docker", "run"], None),
        ("chrt", ["--", "50", "docker", "run"], None),
    ],
)
def test_taskset_dashdash_and_glob_transform_rejected(session, command, args, inner):
    """``taskset``/``chrt`` after ``--`` must still skip the mask/priority to reach docker."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="dashdash-glob", runner="command", command=command, args=args,
            enabled=True,
        )


def test_glob_transform_as_argument_allowed(session):
    """An unresolvable glob transform that is only an ARGUMENT (not the command word) is not a launch
    and must not be rejected."""
    s = service.create_server(
        session, name="glob-arg-ok", runner="command", command="/bin/bash",
        args=["-c", "echo ${D#?}"], enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner", "env"),
    [
        # ``su``/``runuser`` pass ``--session-command`` and clustered ``-lc`` to the shell like -c.
        ("su", ["--session-command", "docker run alpine"], None, None),
        ("su", ["-lc", "docker run alpine"], None, None),
        ("runuser", ["-lc", "docker run"], None, None),
        # ``setpriv [options] PROGRAM`` launches a command.
        ("setpriv", ["--clear-groups", "docker", "run", "alpine"], None, None),
        ("setpriv", ["--reuid", "1000", "docker", "run"], None, None),
    ],
)
def test_session_command_setpriv_find_env_and_command_export_rejected(
        session, command, args, inner, env):
    """``su --session-command``/``-lc`` shell commands and ``setpriv [options] PROGRAM`` fronting
    docker must all be gated."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="session-setpriv-find", runner="command", command=command, args=args,
            env=env or {}, enabled=True,
        )


@pytest.mark.parametrize(
    "inner",
    [
        # Aliases defined on the SAME physical line as their use are not expanded by bash.
        "shopt -s expand_aliases; alias d=docker; d run alpine",
        "shopt -s expand_aliases\nalias d=docker; d run alpine",  # def+use share line 2
    ],
)
def test_same_line_alias_definition_allowed(session, inner):
    """Bash expands aliases while reading each line, so an alias used on the same line it is defined
    stays unexpanded — such a config must not be rejected."""
    s = service.create_server(
        session, name="same-line-alias", runner="command", command="/bin/bash", args=["-c", inner],
        enabled=True,
    )
    assert s.enabled is True


def test_setpriv_and_su_session_command_non_docker_allowed(session):
    """``setpriv``/``su --session-command`` wrapping a non-docker command must not be rejected."""
    for name, cmd, args in [
        ("sp-ok", "setpriv", ["--clear-groups", "python", "-m", "srv"]),
        ("su-sc-ok", "su", ["--session-command", "python -m srv"]),
    ]:
        s = service.create_server(
            session, name=name, runner="command", command=cmd, args=args, enabled=True,
        )
        assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``nsenter [options] PROGRAM`` launches a command.
        ("nsenter", ["--target", "1", "--mount", "docker", "run", "alpine"], None),
        ("nsenter", ["-t", "1", "-m", "docker", "run"], None),
    ],
)
def test_nameref_static_subst_nsenter_and_for_loop_rejected(session, command, args, inner):
    """``nsenter [options] PROGRAM`` fronting docker must be gated."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="nameref-subst-for", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        (None, None, 'declare -n D=E; E=python; "$D" -m srv'),   # nameref resolves to non-docker
        (None, None, "echo $(printf docker)"),                   # substitution output is an argument
        (None, None, '"$(printf \'docker run\')" alpine'),       # quoted multiword isn't a launcher
        ("nsenter", ["--target", "1", "python", "-m", "srv"], None),  # nsenter wrapping non-docker
        (None, None, 'for x in python; do "$x" -m srv; done'),   # loop binds a non-docker value
    ],
)
def test_nameref_static_subst_nsenter_and_for_loop_non_docker_allowed(session, command, args, inner):
    """The nameref/substitution/nsenter/for-loop machinery must not reject the non-docker cases."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    s = service.create_server(
        session, name="nameref-ok", runner="command", command=command, args=args, enabled=True,
    )
    assert s.enabled is True


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        # ``strace [options] COMMAND`` launches the traced command.
        ("strace", ["docker", "run", "alpine"], None),
        ("strace", ["-f", "docker", "run"], None),
        ("strace", ["-o", "/tmp/t", "docker", "run"], None),
    ],
)
def test_indirect_expansion_strace_sourced_pipe_and_read_rejected(session, command, args, inner):
    """``strace [options] COMMAND`` fronting docker must be gated."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="indirect-strace-read", runner="command", command=command, args=args,
            enabled=True,
        )


@pytest.mark.parametrize(
    ("command", "args", "inner"),
    [
        (None, None, 'D=python; x=D; "${!x}" -m srv'),          # indirect resolves to non-docker
        ("strace", ["python", "-m", "srv"], None),              # strace wrapping a non-docker cmd
        (None, None, 'D=python; read X <<< "$D"; "$X" -m srv'),  # read binds a non-docker value
    ],
)
def test_indirect_strace_and_read_non_docker_allowed(session, command, args, inner):
    """The indirect-expansion / strace / read-binding machinery must not reject the non-docker
    cases."""
    if inner is not None:
        command, args = "/bin/bash", ["-c", inner]
    s = service.create_server(
        session, name="indirect-ok", runner="command", command=command, args=args, enabled=True,
    )
    assert s.enabled is True


def test_deeply_nested_shell_command_fails_closed_without_error(session):
    """Pathologically nested ``$(…)`` must not raise (RecursionError) out of the guard; it fails
    closed and rejects the malformed config instead of 500-ing the create path."""
    inner = "$(" * 6000 + "docker run alpine" + ")" * 6000
    with pytest.raises(ValueError, match=_DOCKER_GUARD_MSG):
        service.create_server(
            session, name="deep-nest", runner="command", command="/bin/sh",
            args=["-c", inner], enabled=True,
        )


def test_shell_wrapped_docker_second_c_positional_allowed(session):
    """Only the FIRST ``-c`` supplies the shell command; a later ``-c`` is ``$0`` and its args never
    run, so a docker string there must not trigger a false rejection."""
    s = service.create_server(
        session,
        name="second-c",
        runner="command",
        command="/bin/sh",
        args=["-c", "echo ok", "-c", "docker run alpine"],
        enabled=True,
    )
    assert s.enabled is True


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


def test_update_denied_conversion_leaves_no_dirty_state(session):
    # Codex: the gate raises AFTER the tracked ORM row was reclassified/canonicalized, so a later
    # commit on the same session could flush the DENIED conversion. update_server rolls back on
    # denial — re-reading the row (same session) must still show the original runner.
    s = service.create_server(
        session, name="conv3", runner="command", command="echo", args=["hi"], enabled=True,
    )
    with pytest.raises(ValueError):
        service.update_server(session, s.id, {"runner": "docker", "command": "img:1", "args": []})
    reloaded = repo.get_server(session, s.id)
    assert reloaded.runner == "command" and reloaded.command == "echo"
    # A subsequent commit on the same session must not resurrect the denied conversion.
    session.commit()
    assert repo.get_server(session, s.id).runner == "command"


def test_normalize_docker_servers_gates_non_run_docker_launcher(session):
    # Codex: a legacy local-exec row whose command is the docker CLI but whose args are NOT a
    # recognized `docker run` (e.g. `docker compose run`) must still be converted to the gated
    # docker runner — leaving it as a `command` runner would let it talk to the daemon with the
    # full environment while docker_runner is off.
    s = service.create_server(
        session, name="dc", runner="command", command="echo", args=["hi"], enabled=True,
    )
    row = repo.get_server(session, s.id)
    row.command, row.args = "/usr/bin/docker", ["compose", "run", "mcp"]
    repo.save_server(session, row)
    assert service.normalize_docker_servers(session) == 1
    assert repo.get_server(session, s.id).runner == "docker"  # now gated by the supervisor


def test_normalize_docker_parses_short_context_flag_before_run():
    # Codex: `-c` is the short form of the global `--context`; it must be walked as a global value
    # flag so the real `run` subcommand (and image) is found, and it warns (daemon selection).
    image, args, _, warnings = service.normalize_docker(
        "docker", ["-c", "prod", "run", "ghcr.io/x/y"], {}
    )
    assert image == "ghcr.io/x/y" and args == []
    assert any("daemon" in w for w in warnings)


def test_normalize_docker_warns_on_dropped_config_and_pull():
    # Codex: --config (registry-cred config dir, pre-run) and --pull (pull policy) are dropped;
    # both silently change behavior, so both must surface a review warning.
    _, _, _, w1 = service.normalize_docker("docker", ["--config", "/run/auth", "run", "img"], {})
    assert any("config" in w for w in w1)
    _, _, _, w2 = service.normalize_docker("docker", ["run", "--pull", "always", "img"], {})
    assert any("pull" in w for w in w2)


def test_docker_rejects_proxy_env_key(session):
    # Codex: a container-declared proxy var would land in the docker CLI's own env and could
    # reroute the control-plane's daemon request on a TCP DOCKER_HOST — reject it (both cases).
    _enable_docker(session)
    for bad in ("HTTP_PROXY", "https_proxy", "NO_PROXY", "ALL_PROXY"):
        with pytest.raises(ValueError):
            service.create_server(
                session, name="d", runner="docker", command="img:1", args=[],
                env={bad: "http://evil:3128"}, enabled=False,
            )


def test_normalize_docker_finds_run_when_global_flag_value_is_run():
    # Codex: a global flag whose VALUE is literally "run" (e.g. a context named "run") must not be
    # mistaken for the `run` subcommand. Walk global flags by arity, then the real subcommand.
    image, args, _, _ = service.normalize_docker(
        "docker", ["--context", "run", "run", "ghcr.io/x/y"], {}
    )
    assert image == "ghcr.io/x/y" and args == []
    # inline global flag form: `docker --context=run run img`
    image2, _, _, _ = service.normalize_docker("docker", ["--context=run", "run", "img"], {})
    assert image2 == "img"


def test_normalize_docker_rejects_non_string_arg_tokens():
    # Codex: a pasted/legacy JSON config can carry a non-string arg (e.g. ["run", 123, "img"]).
    # The parser must raise ValueError (callers skip/leave-untouched) rather than AttributeError.
    with pytest.raises(ValueError):
        service.normalize_docker("docker", ["run", 123, "img"], {})


def test_normalize_docker_servers_skips_row_with_bad_arg_token(session):
    # A single legacy docker row with a non-string arg must NOT abort the boot migration — it is
    # left untouched (ValueError swallowed), and other rows still migrate.
    _enable_docker(session)
    bad = service.create_server(
        session, name="bad", runner="docker", command="img:1", args=[], env={}, enabled=False,
    )
    row = repo.get_server(session, bad.id)
    row.command, row.args = "docker", ["run", 123, "img"]  # non-string token
    repo.save_server(session, row)
    # Must not raise (no AttributeError); the malformed row is simply skipped.
    service.normalize_docker_servers(session)
    assert repo.get_server(session, bad.id).command == "docker"  # untouched


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
