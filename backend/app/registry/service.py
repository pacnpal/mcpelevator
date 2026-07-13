"""Desired-state CRUD for MCP servers.

Sits above the repo: generates identity (id/slug), computes the idempotency
``config_hash``, validates the runner, and owns the import/export of the standard
``mcpServers`` JSON shape. Never spawns processes — that's the reconciler's job.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlmodel import Session

from app.db import repo
from app.db.models import RUNNERS, Server
from app.registry import settings as runtime_settings
from app.runners import remote as remote_runner
from app.runners.docker import is_forbidden_container_env, is_reserved_docker_env
from app.runners.remote import canonical_transport
from app.util import config_hash, new_id, slugify

logger = logging.getLogger(__name__)


def _launcher_basename(command: str) -> str:
    """Basename of a launcher path, splitting on BOTH separators regardless of platform.

    ``os.path.basename`` on POSIX leaves a Windows path (``C:\\…\\docker.exe``) intact, so a
    Claude-Desktop-on-Windows config would miss the launcher tables. Normalize both slashes."""
    return command.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()


# --- remote (HTTP/SSE upstream) normalization ----------------------------------
# The transport vocabulary lives in one place (app.runners.remote); we canonicalize
# through it so the stored value is always canonical and config_hash (and therefore
# reconcile) is deterministic regardless of input spelling.
def normalize_remote(command: str, args: Optional[list[str]]) -> tuple[str, list[str]]:
    """Validate + canonicalize a remote server's (url, [transport]).

    ``command`` must be a well-formed ``http(s)://`` URL with a host; ``args[0]`` (if
    any) is the transport, defaulting to ``streamable-http``. Returns the canonical
    pair so the row — and its ``config_hash`` — is deterministic. Raises ``ValueError``
    on a malformed URL or unsupported transport.
    """
    url = (command or "").strip()
    # Parse rather than prefix-match: reject schemeless, hostless (https://), or
    # whitespace-bearing values that would only fail later when the bridge connects.
    # urlsplit lowercases the scheme, so an uppercase HTTPS:// is accepted.
    parsed = urlsplit(url)
    # Check hostname, not just netloc: "https://:443/mcp" has a truthy netloc (":443")
    # but no host, and would only fail later when the bridge tries to connect. Also
    # validate the port — `parsed.port` raises ValueError on a malformed one (":bad").
    try:
        parsed.port  # noqa: B018 — property access validates the port
        valid_port = True
    except ValueError:
        valid_port = False
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not valid_port
        or any(c.isspace() for c in url)
    ):
        raise ValueError("a remote server's command must be an http(s):// URL with a host")
    transport = canonical_transport(args[0] if args else None)
    if transport is None:
        raise ValueError(
            f"remote transport must be one of {list(remote_runner.TRANSPORTS)} "
            f"(got {args[0]!r})"
        )
    return url, [transport]


def normalize_oauth(
    runner: str,
    oauth: bool,
    scopes: Optional[str],
    client_id: Optional[str],
    client_secret: Optional[str],
) -> tuple[bool, str, Optional[str], Optional[str]]:
    """Canonicalize a server's upstream-OAuth config.

    OAuth only applies to the ``remote`` runner (there's no upstream URL to authenticate
    against otherwise), so it is forced off for every other runner and the fields are
    cleared — keeping ``config_hash`` stable and preventing a stray secret from riding
    along on a local server. Blank strings collapse to ``""`` / ``None`` so the stored
    shape is deterministic. A client secret without a client id is meaningless (there's
    no static client to authenticate) — reject it rather than silently drop it.
    """
    if runner != "remote" or not oauth:
        return False, "", None, None
    scopes = (scopes or "").strip()
    client_id = (client_id or "").strip() or None
    client_secret = (client_secret or "").strip() or None
    if client_secret and not client_id:
        raise ValueError("an OAuth client secret requires a client id")
    return True, scopes, client_id, client_secret


# --- docker (OCI image) normalization ------------------------------------------
# The canonical stored shape for a docker server is minimal (SSOT): command = image
# reference, args = the CONTAINER's own arguments, env = the env map. A pasted
# mcpServers entry, though, gives a full `docker run …` invocation; normalize_docker is
# the single place that parses that into the canonical shape so the row (and its
# config_hash) is deterministic and the docker runner can synthesize its own hardened argv.
# Only real docker launchers: the docker runner always execs `docker` (DOCKER_BIN), so we
# must NOT silently reclassify a `podman …` config as this runner — it would run against a
# different daemon (or fail). A podman config instead falls through to the `command` runner,
# which launches it verbatim.
_DOCKER_LAUNCHERS = {"docker", "docker.exe"}

# Runners that execute ``command`` verbatim as a local process (passthrough, no hardening,
# full env). If any of these is pointed straight at the docker CLI it IS the docker runner and
# must be routed through it — otherwise it would launch containers ungated/unhardened with the
# control plane's full environment. (``remote`` is excluded: its command is a URL.)
_LOCAL_EXEC_RUNNERS = {"npx", "uvx", "command"}


def _is_docker_launcher(command: str) -> bool:
    """True only when ``command`` invokes the docker CLI itself.

    A launcher is a bare ``docker``/``docker.exe`` or a filesystem path to it
    (``/usr/local/bin/docker``, ``./docker``, ``~/bin/docker``,
    ``C:\\Program Files\\Docker\\docker.exe``). An OCI image reference whose final path
    segment is literally ``docker`` (the official ``docker`` image, ``docker.io/library/docker``,
    ``ghcr.io/acme/docker``) ALSO has basename ``docker`` but is NOT a launcher — reclassifying it
    or parsing it as a full ``docker run`` invocation would drop the real image and misread the
    container's own args. A registry ref is distinguished from a path because it is neither
    absolute, relative, home-anchored, nor a Windows drive path."""
    c = command.strip()
    if _launcher_basename(c) not in _DOCKER_LAUNCHERS:
        return False
    norm = c.replace("\\", "/")
    if "/" not in norm:
        return True  # a bare launcher name (no path separator) is the CLI
    # A path to the CLI: absolute/relative/home-anchored (POSIX), or a Windows drive path.
    return norm[0] in "/.~" or (len(c) >= 2 and c[1] == ":")


# `docker run` flags that CONSUME the next token as their value (so we skip both). Kept
# reasonably complete for real MCP configs; an unknown value-taking flag is the accepted
# edge (it'd be read as a boolean and its value mistaken for the image — rare in practice).
_DOCKER_VALUE_FLAGS = frozenset({
    "-e", "--env", "--env-file",
    "-a", "--attach",
    "-v", "--volume", "--mount", "--tmpfs",
    "-p", "--publish", "--expose",
    "-w", "--workdir",
    "--name", "--hostname", "-h",
    "--network", "--net", "--network-alias", "--ip", "--ip6", "--link", "--link-local-ip",
    "--add-host", "--dns", "--dns-search", "--dns-option", "--mac-address", "--domainname",
    "--label", "-l", "--label-file",
    "-m", "--memory", "--memory-swap", "--memory-reservation", "--memory-swappiness",
    "--kernel-memory", "--cpus", "--cpuset-cpus", "--cpuset-mems", "--cpu-shares", "-c",
    "--cpu-period", "--cpu-quota", "--cpu-rt-period", "--cpu-rt-runtime", "--blkio-weight",
    "-u", "--user", "--userns", "--group-add", "--cgroup-parent", "--cgroupns",
    "--entrypoint",
    "--platform", "--pull", "--isolation", "--pid", "--ipc", "--uts", "--cidfile",
    "--stop-timeout", "--stop-signal", "--restart", "--detach-keys",
    "--device", "--device-cgroup-rule", "--device-read-bps", "--device-write-bps",
    "--device-read-iops", "--device-write-iops", "--volumes-from", "--volume-driver",
    "--ulimit", "--shm-size", "--pids-limit", "--sysctl", "--storage-opt", "--annotation",
    "--security-opt", "--cap-add", "--cap-drop", "--oom-score-adj",
    "--health-cmd", "--health-interval", "--health-timeout", "--health-retries",
    "--health-start-period", "--health-start-interval",
    "--log-driver", "--log-opt", "--gpus", "--runtime",
})


_ENV_FILE_WARNING = (
    "--env-file is not read — add its variables under Environment so they reach the container."
)

# Mount-family flags: dropped by the hardened runner (it doesn't bind host paths). Warn so a
# config that depended on a mount isn't silently imported as a broken server.
_DOCKER_MOUNT_FLAGS = frozenset({"-v", "--volume", "--mount", "--tmpfs"})

# --entrypoint is dropped too (the runner owns the invocation and can't override ENTRYPOINT),
# so a config relying on a custom entrypoint would silently start the image's default process.
_ENTRYPOINT_WARNING = (
    "--entrypoint is dropped — the docker runner uses the image's default entrypoint, so the "
    "server may start the wrong process."
)


def _mount_warning(flag: str) -> str:
    return (
        f"{flag} (a host mount) is dropped — the docker runner doesn't bind host paths; the "
        f"server may miss files/data it expected."
    )


# More host-side `docker run` flags the hardened runner owns (and therefore drops on import).
# Each silently changes behavior the operator likely intended, so surface a review warning
# rather than importing a quietly-wrong server.
_WORKDIR_FLAGS = frozenset({"-w", "--workdir"})
_NETWORK_FLAGS = frozenset({"--network", "--net"})
_USER_FLAGS = frozenset({"-u", "--user"})
_WORKDIR_WARNING = (
    "-w/--workdir is dropped — the docker runner uses the image's own WORKDIR, so a relative "
    "command/entrypoint may run from the wrong directory."
)
_NETWORK_WARNING = (
    "--network is dropped — the docker runner uses Docker's default bridge (egress ON), so a "
    "config that set --network none (isolation) or a custom/attached network no longer has it."
)
_PLATFORM_WARNING = (
    "--platform is dropped — the docker runner uses the host architecture, so a config pinning a "
    "platform (e.g. linux/amd64 on arm64) may pull the wrong image or fail with exec-format."
)
_PULL_WARNING = (
    "--pull is dropped — the docker runner uses Docker's default pull policy, so a config that set "
    "--pull always (refreshed tag) or --pull never (offline/reproducible) no longer applies."
)
_USER_WARNING = (
    "-u/--user is dropped — the docker runner runs as the image's default user, so a config that "
    "pinned a UID/GID (least-privilege or file permissions) no longer applies."
)
# --read-only is a BOOLEAN flag (no value), so it's caught in the boolean-skip path, not the
# value-flag branch — but dropping it silently weakens a config that hardened the rootfs.
_READ_ONLY_WARNING = (
    "--read-only is dropped — the hardened runner leaves the container root filesystem writable "
    "(Docker's default), so a config that made it read-only no longer does."
)


def _dropped_flag_warning(flag: str) -> Optional[str]:
    """Review warning for a ``docker run`` flag the hardened runner drops on import, or ``None``.

    The runner owns the whole invocation (it stores only image + container args), so every
    host-side run flag in a pasted config is dropped. Most are benign — they duplicate a
    hardening default or are meaningless under stdio — but a handful silently change intended
    behavior (a host mount, a custom entrypoint, an env-file, a workdir, network isolation, a
    pinned platform, a pull policy, a pinned user, a read-only rootfs); surface those so the
    imported server isn't quietly broken or silently weakened."""
    if flag in _DOCKER_MOUNT_FLAGS:
        return _mount_warning(flag)
    if flag == "--entrypoint":
        return _ENTRYPOINT_WARNING
    if flag == "--env-file":
        return _ENV_FILE_WARNING
    if flag in _WORKDIR_FLAGS:
        return _WORKDIR_WARNING
    if flag in _NETWORK_FLAGS:
        return _NETWORK_WARNING
    if flag == "--platform":
        return _PLATFORM_WARNING
    if flag == "--pull":
        return _PULL_WARNING
    if flag in _USER_FLAGS:
        return _USER_WARNING
    if flag == "--read-only":
        return _READ_ONLY_WARNING
    return None


# Global docker flags (BEFORE the subcommand) that consume the NEXT token as their value. Walking
# these by arity is what lets us find the real `run` subcommand even when a flag's value is itself
# the word "run" (e.g. a one-off context named "run": `docker --context run run img`).
_DOCKER_GLOBAL_VALUE_FLAGS = frozenset({
    "-H", "--host", "-l", "--log-level", "-c", "--context", "--config",
    "--tlscacert", "--tlscert", "--tlskey",
})


def _docker_run_index(tokens: list[str]) -> Optional[int]:
    """Index just AFTER the ``run`` subcommand in a docker arg list, or ``None`` if this isn't
    a ``docker run`` invocation.

    Recognizes ``docker run …``, the ``docker container run …`` long form, and leading global
    flags (``docker --context X run …``) — everything before ``run`` configures the CLI/daemon,
    not the container, so it's dropped. The pre-subcommand global flags are walked by VALUE ARITY
    (not a naive ``tokens.index("run")``) so a flag whose value is literally ``run`` — e.g. a
    context named ``run`` — isn't mistaken for the subcommand. Returns ``None`` (→ treat the
    command as a bare image ref) when the first non-flag token isn't ``run``/``container``, so an
    image whose basename is "docker" with container args isn't misparsed as a launcher."""
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if not isinstance(tok, str):
            return None  # malformed token — caller treats command as a bare image ref
        if tok.startswith("-"):
            # A leading GLOBAL flag: skip it, plus its value when it takes one in the separated
            # form (inline ``--flag=value`` carries its own value, so it never eats the next token).
            if "=" not in tok and tok in _DOCKER_GLOBAL_VALUE_FLAGS:
                i += 2
            else:
                i += 1
            continue
        # First non-flag token is the subcommand.
        if tok == "container" and i + 1 < n and tokens[i + 1] == "run":
            return i + 2
        if tok == "run":
            return i + 1
        return None  # a plain word that isn't run/container — not a `docker run` (bare image ref)
    return None


# Global CLI flags (before `run`) that RETARGET which Docker daemon the command talks to. The
# runner always uses mcpelevator's own configured daemon (DOCKER_HOST), so these are dropped —
# but silently switching daemons is exactly the kind of change to surface for review.
_DAEMON_SELECT_FLAGS = frozenset({"-H", "--host", "-c", "--context"})
_DAEMON_WARNING = (
    "a daemon-selection flag (--context/-c/-H/--host) before `run` is dropped — the docker runner "
    "always targets mcpelevator's own configured Docker daemon (DOCKER_HOST), so the server may "
    "run on a different daemon than the pasted config selected."
)
# --config selects the CLI config DIRECTORY (which can hold registry credentials); dropping it
# means a private-image pull falls back to mcpelevator's own docker config and may fail.
_CONFIG_SELECT_FLAGS = frozenset({"--config"})
_CONFIG_WARNING = (
    "--config (a docker CLI config dir) before `run` is dropped — the docker runner uses "
    "mcpelevator's own docker config, so registry credentials in that config aren't used and a "
    "private-image pull may fail."
)


def _pre_run_flag_present(pre_run_tokens: list[str], flags: frozenset[str]) -> bool:
    """True if the tokens before ``run`` include any of ``flags`` (exact ``--context prod`` /
    ``-H tcp://…`` or inline ``--context=prod`` / ``--config=…``)."""
    return any(tok.split("=", 1)[0] in flags for tok in pre_run_tokens)


def _capture_env(token: str, env: dict[str, str], warnings: list[str]) -> None:
    """Fold a ``-e`` argument into the env map.

    ``VAR=value`` provides a value (the explicit env map still wins, via setdefault).
    A bare ``VAR`` (name-only passthrough) relied on a host env var in the original config;
    we can't read that, so we **scaffold** it as ``VAR=""`` and warn — this keeps the key
    present (the builder emits ``-e VAR`` and it shows up in the review form to fill in),
    rather than silently dropping a required secret so the server starts without it."""
    name, sep, val = token.partition("=")
    if not name:
        return
    if sep:
        env.setdefault(name, val)
    elif name not in env:
        env[name] = ""  # scaffold for review; the operator fills the value before enabling
        warnings.append(
            f"-e {name} relied on a host environment variable — set {name}'s value under "
            f"Environment before starting."
        )


def normalize_docker(
    command: str, args: Optional[list[str]], env: Optional[dict[str, str]]
) -> tuple[str, list[str], dict[str, str], list[str]]:
    """Normalize a docker server to the canonical (image, container_args, env) shape.

    Accepts either a full invocation (``command`` basename in docker/podman, ``args`` the
    ``run …`` line) or an already-bare image ref (``command`` = image, ``args`` = container
    args). Returns ``(image, container_args, env, warnings)``. Raises ``ValueError`` when no
    image can be found. Pure and deterministic — the same input always yields the same shape.
    """
    env = dict(env or {})
    warnings: list[str] = []
    tokens = list(args or [])
    # ``args`` can carry non-string items from a pasted/legacy JSON config (e.g. ["run", 123, …]);
    # the token-walk below calls string methods, so reject a malformed list as a ValueError rather
    # than letting an AttributeError escape. Callers treat ValueError as "skip/leave untouched" —
    # importantly the boot migration, so one bad stored row can't abort startup.
    if any(not isinstance(t, str) for t in tokens):
        raise ValueError("a docker server's args must all be strings")

    # Treat this as a full `docker run …` invocation only when the command actually invokes the
    # docker CLI (bare name or a filesystem path — see _is_docker_launcher) AND the args are a
    # recognized `run` invocation (see _docker_run_index). Otherwise the command IS an image ref
    # whose basename merely happens to be "docker" (the official `docker` image,
    # `ghcr.io/acme/docker`) with container args — preserve it rather than dropping it.
    run_idx = _docker_run_index(tokens) if _is_docker_launcher(command or "") else None
    if run_idx is None:
        image = (command or "").strip()
        if not image:
            raise ValueError("a docker server needs an image reference")
        return image, tokens, env, warnings

    # Leading global flags (before `run`) are dropped — they configure the CLI/daemon, not the
    # container. Most are inert, but a daemon/context selector (--context/-c/-H/--host) or a
    # config-dir selector (--config, which can carry registry creds) silently changes behavior,
    # so warn: the runner always uses mcpelevator's own daemon + docker config.
    pre_run = tokens[: run_idx - 1]
    if _pre_run_flag_present(pre_run, _DAEMON_SELECT_FLAGS):
        warnings.append(_DAEMON_WARNING)
    if _pre_run_flag_present(pre_run, _CONFIG_SELECT_FLAGS):
        warnings.append(_CONFIG_WARNING)

    # Full invocation: start after the `run` subcommand (leading global flags / `container` are
    # dropped — they configure the CLI/daemon, not the container), then token-walk the run flags.
    i = run_idx

    image: Optional[str] = None
    container_args: list[str] = []
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":  # explicit end of options: next token is the image
            i += 1
            if i < n:
                image = tokens[i]
                container_args = tokens[i + 1:]
            break
        if tok.startswith("-"):
            # Attached short form: `-eNAME` / `-eNAME=val` (docker allows `-e` with no space,
            # and reads `-eNAME` as `-e NAME`). Handle before the generic flag branches so the
            # variable isn't dropped. `-e` alone and `-e=…` fall through to the branches below.
            if tok.startswith("-e") and not tok.startswith(("--", "-e=")) and tok != "-e":
                _capture_env(tok[2:], env, warnings)
                i += 1
                continue
            if "=" in tok:  # inline value form, e.g. --env=VAR / --network=none / --memory=1g
                name, _, val = tok.partition("=")
                if name in ("-e", "--env"):
                    _capture_env(val, env, warnings)
                else:
                    warn = _dropped_flag_warning(name)
                    if warn:
                        warnings.append(warn)
                i += 1
                continue
            if tok in ("-e", "--env"):
                if i + 1 < n:
                    _capture_env(tokens[i + 1], env, warnings)
                    i += 2
                else:
                    i += 1
                continue
            if tok in ("-d", "--detach"):
                warnings.append("-d/--detach is incompatible with stdio and is ignored.")
                i += 1
                continue
            if tok in ("--privileged",):
                warnings.append("--privileged is dropped by the hardened docker runner.")
                i += 1
                continue
            if tok in _DOCKER_VALUE_FLAGS:
                # Skip the flag and its value; warn for the ones whose loss silently changes
                # behavior (mount, entrypoint, env-file, workdir, network, platform).
                warn = _dropped_flag_warning(tok)
                if warn:
                    warnings.append(warn)
                i += 2
                continue
            # A boolean flag (-i, -t, -it, --rm, --init, …); skip it. A few booleans still warn
            # when dropped changes intended behavior (e.g. --read-only weakens the rootfs).
            warn = _dropped_flag_warning(tok)
            if warn:
                warnings.append(warn)
            i += 1
            continue
        # First non-flag token is the image; everything after it is the container's args.
        image = tok
        container_args = tokens[i + 1:]
        break

    if not image:
        raise ValueError("a docker server's args must include an image reference")
    return image, container_args, env, warnings


def _validate_docker_env(env: dict[str, str]) -> None:
    """Reject env names that would break the docker runner's no-values-in-argv guarantee.

    The docker builder emits ``-e KEY`` (name only); a key containing ``=`` or whitespace
    would become ``-e KEY=value`` in the argv (leaking the value into ``ps``/``inspect``) or
    an invalid child-env name. A key that collides with the bridge's reserved docker
    connection vars (``DOCKER_HOST`` etc.) would pass the CONTROL daemon endpoint into the
    untrusted container (name-only ``-e`` reads it from the CLI's own env). Reject both at
    the boundary so they never persist."""
    for key in env:
        if "=" in key or any(c.isspace() for c in key):
            raise ValueError(f"invalid environment variable name {key!r} for a docker server")
        if is_reserved_docker_env(key):
            raise ValueError(
                f"{key!r} is reserved for the docker runner and can't be a container env var"
            )
        if is_forbidden_container_env(key):  # a Go proxy var (is_reserved handled just above)
            raise ValueError(
                f"{key!r} can't be a docker container env var: it would alter the docker CLI's own "
                f"HTTP proxy — use the docker CLI's `proxies` config to proxy launched containers"
            )


def _validate_docker_image(image: str) -> None:
    """Reject an image reference that could inject `docker run` options.

    ``build()`` emits the image as a positional after ``--`` (which neutralizes it), but a
    leading-dash image is invalid anyway and, on a legacy row without the ``--`` guard, would
    be parsed by docker as an extra option (``--volume=/:/host``, ``--privileged``, …) —
    bypassing every hardening flag. Reject it at the boundary so it can never persist."""
    if not image or image.startswith("-"):
        raise ValueError("a docker image reference can't be empty or start with '-'")


def _normalize_validate_docker(
    command: str, args: Optional[list[str]], env: Optional[dict[str, str]], *, name: str
) -> tuple[str, list[str], dict[str, str], list[str]]:
    """Shared docker canonicalization for ``create_server`` / ``update_server``: parse a pasted
    invocation to the canonical (image, container_args, env) shape, validate the image + env, and
    log the dropped-flag warnings. Returns ``(command, args, env, warnings)``. The callers own the
    parts that differ per path — the host-cwd reset, the ``warnings_sink`` surfacing (create), and
    the enabled-transition gate (create vs. the rollback-on-deny convert gate in update) — so this
    single seam keeps the two entry points from drifting on any future hardening check."""
    command, args, env, warnings = normalize_docker(command, args, env)
    _validate_docker_image(command)
    _validate_docker_env(env)
    for w in warnings:  # the hardened parser altered the invocation — surface it, don't do it silently
        logger.warning("docker server %r: %s", name, w)
    return command, args, env, warnings


def _require_docker_enabled(session: Session) -> None:
    """Gate the root-equivalent docker runner behind the opt-in ``docker_runner`` setting.

    Raised as ``ValueError`` so the API surfaces a 400. Enforced on the transition to
    *enabled* (create-with-enabled, enable, update-while-enabled); importing/creating a
    disabled docker server is always allowed so a paste can be reviewed first."""
    if not runtime_settings.docker_runner(session):
        raise ValueError(
            "Docker runner is disabled — enable it in Settings first (it is root-equivalent)."
        )


def compute_hash(server: Server) -> str:
    return config_hash(
        {
            "runner": server.runner,
            "command": server.command,
            "args": server.args,
            "env": server.env,
            "cwd": server.cwd,
            "mcp_http": server.mcp_http,
            "rest_openapi": server.rest_openapi,
            # OAuth config drives how the bridge authenticates upstream, so it IS part
            # of the launch spec — a change must restart the bridge. (The tokens live in
            # a file store, not the row, so *authenticating* leaves the hash untouched.)
            # The client SECRET is deliberately NOT read here: it's a credential and must
            # never flow into a fast digest, and the bridge doesn't consume it from the
            # spec anyway (it reads the DCR/static client_info from the token store, and a
            # secret change re-runs auth via the API which clears the tokens). The static
            # client is already tracked by the non-sensitive client_id below.
            "oauth": server.oauth,
            "oauth_scopes": server.oauth_scopes,
            "oauth_client_id": server.oauth_client_id,
            # auth_provider is intentionally excluded: it's enforced at the proxy
            # per-request, so changing it must NOT restart the bridge process.
        }
    )


def backfill_config_hashes(session: Session) -> int:
    """Recompute ``config_hash`` for stored servers so rows written by an older
    version (with a different hash-input shape — e.g. ``auth_provider`` was once
    included) are rehashed to the current shape. Without this, the first
    non-hash-affecting PATCH on an upgraded server would change the stored hash and
    trigger a spurious bridge restart. Idempotent — only writes rows whose hash
    actually changed. Returns how many were updated."""
    changed = 0
    for server in repo.list_servers(session):
        new_hash = compute_hash(server)
        if new_hash != server.config_hash:
            repo.set_config_hash(session, server.id, new_hash)
            changed += 1
    return changed


def _scrub_docker_env(env: dict[str, str]) -> dict[str, str]:
    """Drop env keys that ``_validate_docker_env`` would reject (malformed names, the bridge's
    reserved DOCKER_*/PATH connection vars, or a Go proxy var). Used by the boot migration,
    which can't raise on a legacy row the way create/update can."""
    return {
        k: v for k, v in env.items()
        if "=" not in k and not any(c.isspace() for c in k) and not is_forbidden_container_env(k)
    }


def normalize_docker_servers(session: Session) -> int:
    """Canonicalize legacy docker rows so enabling one is gated, hardened, and launches the
    right image.

    Two legacy shapes from a prior release (where the docker runner only raised):
    (1) ``runner="docker"`` rows stored verbatim (``command="docker"``, ``args=["run", …]``);
    (2) rows stored under a local-exec runner (``command``/``npx``/``uvx``) because the old
        ``_infer_runner`` matched only the literal ``"docker"`` — so ``command="/usr/local/bin/docker"``
        imports slipped through as generic passthrough servers that would bypass the docker gate +
        hardening. An enabled ``runner="npx"`` row with ``command="/usr/bin/docker"`` would otherwise
        never hit the ``sv.runner == "docker"`` reconcile gate and launch ungated with the full env.
    ANY local-exec row whose command is the docker CLI is converted — not only a recognized
    ``docker run`` (matching create/update, which reclassify on the launcher alone). A row like
    ``command="/usr/bin/docker", args=["compose", "run", …]`` isn't a supported ``docker run``, but
    leaving it as a ``command`` runner would let it talk to the daemon with the full environment
    while the gate is off; converting it to ``runner="docker"`` gates it (it then fails to launch a
    real image, which is correct — that shape was never a working MCP server).
    Both are re-normalized to the canonical (image, container_args, env) shape, converted to
    ``runner="docker"``, and have reserved/malformed env keys scrubbed. A row that's already
    canonical re-normalizes to itself (no write). Idempotent. Returns the count changed. A
    row that can't be parsed (no image) is left untouched — enabling it surfaces the error."""
    changed = 0
    for server in repo.list_servers(session):
        # Gate on the LAUNCHER alone (not a recognized `docker run`): a docker-CLI command with
        # any args must be routed through the gated runner, else it runs ungated as passthrough.
        is_docker_cmd = _is_docker_launcher(server.command or "")
        if server.runner == "docker":
            pass  # always re-normalize existing docker rows (idempotent for canonical ones)
        elif server.runner in _LOCAL_EXEC_RUNNERS and is_docker_cmd:
            pass  # a docker-CLI command misclassified as a passthrough runner — convert + gate it
        else:
            continue
        try:
            image, args, env, _ = normalize_docker(server.command, server.args, server.env)
        except ValueError:
            continue
        env = _scrub_docker_env(env)
        if (
            server.runner != "docker"
            or image != server.command
            or args != list(server.args or [])
            or env != dict(server.env or {})
            or server.cwd is not None
        ):
            server.runner = "docker"
            server.command, server.args, server.env, server.cwd = image, args, env, None
            server.config_hash = compute_hash(server)
            repo.save_server(session, server)
            changed += 1
    return changed


_AUTH_PROVIDERS = {"inherit", "none", "bearer"}


def normalize_auth_providers(session: Session) -> int:
    """Canonicalize stored ``auth_provider`` values from older versions (the old API
    schema accepted any ``str``): ``"Bearer"`` / ``"bearer "`` -> ``"bearer"``, and an
    unresolvable value -> ``"inherit"`` (the admin-controlled default). Without this,
    the dashboard/copy snippets advertise a usable endpoint while ``resolve()`` 403s
    the raw value on every ``/s/...`` request. Idempotent. Returns the count changed."""
    changed = 0
    for server in repo.list_servers(session):
        norm = (server.auth_provider or "").strip().lower()
        if norm not in _AUTH_PROVIDERS:
            norm = "inherit"
        if norm != server.auth_provider:
            repo.set_auth_provider(session, server.id, norm)
            changed += 1
    return changed


# Slugs that would collide with a sibling literal segment on the proxy/API routes
# and shadow it. A server slugged "summary" would capture GET /api/health/summary
# (the aggregate route) so its own /api/health/{slug} could never be reached, and a
# load balancer would read the aggregate instead of that server's status. Reserved
# here so such a name is disambiguated (e.g. "summary" -> "summary-2") at creation.
_RESERVED_SLUGS = frozenset({"summary"})


def _unique_slug(session: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while slug in _RESERVED_SLUGS or repo.get_server_by_slug(session, slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _validate_slug(session: Session, raw: str, *, current_id: str) -> str:
    """Normalize and validate an operator-chosen slug for an existing server.

    Unlike ``_unique_slug`` (which silently disambiguates at creation), an explicit
    rename must surface a conflict rather than guess: the operator picked this URL,
    so a reserved word or a slug already taken by *another* server is a hard error.
    Re-using the server's own current slug is a no-op and allowed.
    """
    slug = slugify(raw)
    if slug in _RESERVED_SLUGS:
        raise ValueError(f"slug {slug!r} is reserved")
    existing = repo.get_server_by_slug(session, slug)
    if existing is not None and existing.id != current_id:
        raise ValueError(f"slug {slug!r} is already in use")
    return slug


def create_server(
    session: Session,
    *,
    name: str,
    runner: str,
    command: str,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    mcp_http: bool = True,
    rest_openapi: bool = False,
    auth_provider: str = "inherit",
    oauth: bool = False,
    oauth_scopes: str = "",
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    enabled: bool = False,
    source: str = "manual",
    warnings_sink: Optional[list[str]] = None,
) -> Server:
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r}; must be one of {RUNNERS}")
    if not command.strip():
        raise ValueError("command is required")
    # A local-exec runner (npx/uvx/command) pointed at the docker CLI IS the docker runner —
    # route it through the docker machinery (normalize + validate + gate + hardening +
    # minimal_env) so it can't launch containers ungated/unhardened with the full control-plane
    # env (choosing a different runner string must not sidestep the root-equivalent gate).
    if runner in _LOCAL_EXEC_RUNNERS and _is_docker_launcher(command):
        runner = "docker"
    # A remote server reuses command/args for the upstream URL + transport; canonicalize
    # them up front so the persisted row (and config_hash) is deterministic. There is no
    # local process, so a working directory is meaningless — drop it.
    if runner == "remote":
        command, args = normalize_remote(command, args)
        cwd = None
    # OAuth applies only to remote; normalize (and force-off elsewhere) before hashing.
    oauth, oauth_scopes, oauth_client_id, oauth_client_secret = normalize_oauth(
        runner, oauth, oauth_scopes, oauth_client_id, oauth_client_secret
    )
    # A docker server is stored in canonical (image, container_args, env) shape; a pasted
    # `docker run …` invocation is parsed down to it. The gate bites only when the server
    # is created already enabled — a disabled import stays reviewable.
    if runner == "docker":
        command, args, env, warnings = _normalize_validate_docker(command, args, env, name=name)
        cwd = None  # a container has its own filesystem; a host cwd is meaningless
        if warnings_sink is not None:  # let callers (import) surface these to the operator, not just logs
            warnings_sink.extend(warnings)
        if enabled:
            _require_docker_enabled(session)

    server = Server(
        id=new_id(),
        slug=_unique_slug(session, name),
        name=name.strip() or "server",
        runner=runner,
        command=command.strip(),
        args=list(args or []),
        env=dict(env or {}),
        cwd=cwd,
        mcp_http=mcp_http,
        rest_openapi=rest_openapi,
        auth_provider=auth_provider,
        oauth=oauth,
        oauth_scopes=oauth_scopes,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        enabled=enabled,
        source=source,
    )
    server.config_hash = compute_hash(server)
    return repo.create_server(session, server)


_MUTABLE_FIELDS = {
    "name",
    "runner",
    "command",
    "args",
    "env",
    "cwd",
    "mcp_http",
    "rest_openapi",
    "auth_provider",
    "oauth",
    "oauth_scopes",
    "oauth_client_id",
    "oauth_client_secret",
}


def update_server(session: Session, server_id: str, changes: dict[str, Any]) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    # Pre-edit runner: converting a NON-docker server INTO a docker one newly grants the
    # root-equivalent runner, so it must be gated like create/enable. Merely editing a row that
    # was ALREADY docker is not gated (so a broken image/env can be fixed while the runner is
    # off). Captured before the mutation/reclassify below changes server.runner.
    was_docker = server.runner == "docker"
    # slug is identity/routing, not launch config: validated separately and excluded
    # from config_hash, so a rename re-routes the proxy without bouncing the bridge.
    if "slug" in changes:
        server.slug = _validate_slug(session, changes["slug"], current_id=server.id)
    for key, value in changes.items():
        if key in _MUTABLE_FIELDS:
            setattr(server, key, value)
    if server.runner not in RUNNERS:
        raise ValueError(f"unknown runner {server.runner!r}")
    # A local-exec runner (npx/uvx/command) pointed at the docker CLI IS the docker runner (see
    # create) — reclassify so it can't launch containers ungated/unhardened via passthrough.
    if server.runner in _LOCAL_EXEC_RUNNERS and _is_docker_launcher(server.command):
        server.runner = "docker"
    if server.runner == "remote":
        server.command, server.args = normalize_remote(server.command, server.args)
        # Converting a local server to remote: PATCH drops the form's cwd:null, so clear
        # the stale working directory here (remote has no process) to keep the row canonical.
        server.cwd = None
    # Normalize OAuth (and force it off for any non-remote runner) so a stray secret can't
    # ride along on a converted server and the hash stays deterministic.
    server.oauth, server.oauth_scopes, server.oauth_client_id, server.oauth_client_secret = (
        normalize_oauth(
            server.runner,
            bool(server.oauth),
            server.oauth_scopes,
            server.oauth_client_id,
            server.oauth_client_secret,
        )
    )
    if server.runner == "docker":
        server.command, server.args, server.env, _ = _normalize_validate_docker(
            server.command, server.args, server.env, name=server.name
        )
        # Converting a local server (with a cwd) to docker: PATCH drops the form's cwd:null,
        # so clear the now-meaningless working directory here to keep the row canonical.
        server.cwd = None
        # Gate a non-docker -> docker CONVERSION on an already-ENABLED row. PATCH can't set
        # enabled=false, so the row stays enabled; without this gate it would start unreviewed
        # the moment the global docker_runner setting is toggled on (the supervisor marks it
        # failed only while the setting is off). Editing a row that was already docker stays
        # ungated (fix a broken image/env offline); enabling a disabled row is gated in
        # set_enabled; creating enabled is gated in create_server.
        if server.enabled and not was_docker:
            try:
                _require_docker_enabled(session)
            except ValueError:
                # The tracked ORM row is already mutated (reclassified/canonicalized) above. Roll
                # back so the DENIED conversion can't be flushed by a later commit on this same
                # session, which would persist runner=docker on a still-enabled row.
                session.rollback()
                raise
    server.config_hash = compute_hash(server)  # recompute -> drives idempotent reconcile
    return repo.save_server(session, server)


def clone_server(session: Session, server_id: str, *, name: Optional[str] = None) -> Server:
    """Create a new server from an existing one's launch + exposure config.

    The clone gets a fresh id and a unique slug derived from its name, and is always
    created disabled (the operator reviews, then enables) so two identical servers
    never race to bind/serve. Pass ``name`` to label the copy; defaults to
    ``"<source> copy"``. Raises ``KeyError`` if the source doesn't exist.
    """
    src = repo.get_server(session, server_id)
    if src is None:
        raise KeyError(server_id)
    new_name = (name or "").strip() or f"{src.name} copy"
    return create_server(
        session,
        name=new_name,
        runner=src.runner,
        command=src.command,
        # Tolerate a NULL JSON column from a legacy/hand-edited row (the model
        # types these non-optional, but the DB can still hold null).
        args=list(src.args or []),
        env=dict(src.env or {}),
        cwd=src.cwd,
        mcp_http=src.mcp_http,
        rest_openapi=src.rest_openapi,
        auth_provider=src.auth_provider,
        oauth=bool(src.oauth),
        oauth_scopes=src.oauth_scopes or "",
        oauth_client_id=src.oauth_client_id,
        oauth_client_secret=src.oauth_client_secret,
        enabled=False,
        source="clone",
    )


def set_enabled(session: Session, server_id: str, enabled: bool) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    # Enabling a docker server is the point the root-equivalent gate bites (import/create
    # left it disabled and reviewable).
    if enabled and server.runner == "docker":
        _require_docker_enabled(session)
    server.enabled = enabled
    return repo.save_server(session, server)


# Node/Python launchers we recognize so the runner badge is meaningful. Anything
# else is stored as a generic `command` (still launched verbatim).
_NPX_LAUNCHERS = {"npx", "npx.cmd", "bunx", "pnpm", "node"}
_UVX_LAUNCHERS = {"uvx", "uv"}


def _infer_runner(command: str) -> str:
    # Match on the basename so an absolute launcher path (e.g. "/usr/local/bin/docker",
    # as Claude Desktop configs commonly write) infers the right runner, not "command".
    base = _launcher_basename(command)
    if base in _NPX_LAUNCHERS:
        return "npx"
    if base in _UVX_LAUNCHERS:
        return "uvx"
    if _is_docker_launcher(command):
        return "docker"
    return "command"


def import_mcp_servers(
    session: Session, data: dict
) -> tuple[list[Server], list[dict], list[dict]]:
    """Create servers from the standard Claude-Desktop ``mcpServers`` JSON shape.

    Accepts either ``{"mcpServers": {...}}`` or a bare ``{name: {...}}`` map.
    Stdio entries (``command`` + ``args`` + ``env``) become local servers; remote
    entries (``url`` / ``type: sse|streamable-http|http``) become ``remote`` servers
    that proxy the upstream URL. All are stored verbatim and disabled (the user
    reviews, then enables).

    Returns ``(created, skipped, warnings)``. ``warnings`` is a list of
    ``{"name", "warnings": [...]}`` — non-fatal notes for a created (disabled) server the
    operator should see BEFORE enabling, chiefly a docker ``run`` option the hardened runner
    dropped (mount, ``--network none``, ``--env-file``, …). These are also logged, but the
    import response surfaces them so the reviewer isn't blind to the transformation.
    """
    servers_map = data.get("mcpServers") if isinstance(data, dict) and "mcpServers" in data else data
    if not isinstance(servers_map, dict):
        raise ValueError("expected an object with an 'mcpServers' map of servers")

    created: list[Server] = []
    skipped: list[dict] = []
    warnings: list[dict] = []
    for name, entry in servers_map.items():
        if not isinstance(entry, dict):
            skipped.append({"name": str(name), "reason": "entry is not an object"})
            continue
        # Accept the URL under "url" or Gemini CLI's "httpUrl" (its Streamable-HTTP shape,
        # which our own install snippets emit — see frontend/src/lib/install.ts).
        url = entry.get("url") or entry.get("httpUrl")
        # mcpServers configs spell the remote transport as either "type" or "transport";
        # a bare httpUrl implies streamable-http.
        etype = entry.get("type") or entry.get("transport")
        if etype is None and entry.get("httpUrl") and not entry.get("url"):
            etype = "streamable-http"
        if url or etype in ("sse", "streamable-http", "http"):
            # A remote (already-HTTP) MCP server: elevate it as a proxied remote runner.
            if not url:
                skipped.append({"name": str(name), "reason": "remote entry has no url"})
                continue
            try:
                created.append(
                    create_server(
                        session,
                        name=str(name),
                        runner="remote",
                        command=str(url),
                        args=[str(etype)] if etype else [],
                        # mcpServers remote entries carry auth as `headers`; fall back to
                        # `env` for tools that (incorrectly) reuse it for header values.
                        env=dict(entry.get("headers") or entry.get("env") or {}),
                        source="import",
                        enabled=False,
                    )
                )
            # A malformed entry (e.g. non-mapping `headers`/`env` → dict() raises
            # TypeError, or a bad URL/transport → ValueError) is skipped, never fatal.
            except (ValueError, TypeError) as exc:
                skipped.append({"name": str(name), "reason": str(exc)})
            continue
        command = entry.get("command")
        if not command:
            skipped.append({"name": str(name), "reason": "no command to launch"})
            continue
        sink: list[str] = []
        try:
            created.append(
                create_server(
                    session,
                    name=str(name),
                    runner=_infer_runner(command),
                    command=str(command),
                    args=list(entry.get("args") or []),
                    env=dict(entry.get("env") or {}),
                    source="import",
                    enabled=False,
                    warnings_sink=sink,
                )
            )
            if sink:  # a docker paste whose dropped run-options the operator must know about
                warnings.append({"name": str(name), "warnings": sink})
        except (ValueError, TypeError) as exc:
            skipped.append({"name": str(name), "reason": str(exc)})
    return created, skipped, warnings
