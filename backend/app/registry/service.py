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
from app.runners.docker import is_reserved_docker_env
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


def _docker_run_index(tokens: list[str]) -> Optional[int]:
    """Index just AFTER the ``run`` subcommand in a docker arg list, or ``None`` if this isn't
    a ``docker run`` invocation.

    Recognizes ``docker run …``, the ``docker container run …`` long form, and a leading run of
    global flags (``docker --context X run …``) — everything before ``run`` configures the
    CLI/daemon, not the container, so it's dropped. Returns ``None`` (→ treat the command as a
    bare image ref) when the first arg is a plain word that isn't ``run``/``container``, so an
    image whose basename is "docker" with container args isn't misparsed as a launcher."""
    if not tokens or "run" not in tokens:
        return None
    first = tokens[0]
    if first == "run" or first == "container" or first.startswith("-"):
        return tokens.index("run") + 1
    return None


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
    base = _launcher_basename(command or "")

    # Treat this as a full `docker run …` invocation only when the command's basename is a
    # docker launcher AND the args are a recognized `run` invocation (see _docker_run_index:
    # `docker run …`, `docker container run …`, or global flags before `run`). Otherwise the
    # command IS an image ref whose basename merely happens to be "docker" (the official
    # `docker` image, `ghcr.io/acme/docker`) with container args — preserve it.
    run_idx = _docker_run_index(tokens) if base in _DOCKER_LAUNCHERS else None
    if run_idx is None:
        image = (command or "").strip()
        if not image:
            raise ValueError("a docker server needs an image reference")
        return image, tokens, env, warnings

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
            if "=" in tok:  # inline value form, e.g. --env=VAR / -e=VAR / --env-file=… / --memory=1g
                name, _, val = tok.partition("=")
                if name in ("-e", "--env"):
                    _capture_env(val, env, warnings)
                elif name == "--env-file":
                    warnings.append(_ENV_FILE_WARNING)
                elif name in _DOCKER_MOUNT_FLAGS:
                    warnings.append(_mount_warning(name))
                elif name == "--entrypoint":
                    warnings.append(_ENTRYPOINT_WARNING)
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
                if tok == "--env-file":
                    # We can't read the file; surface it like a bare -e so its vars aren't
                    # silently lost (the server would otherwise start with no environment).
                    warnings.append(_ENV_FILE_WARNING)
                elif tok in _DOCKER_MOUNT_FLAGS:
                    warnings.append(_mount_warning(tok))
                elif tok == "--entrypoint":
                    warnings.append(_ENTRYPOINT_WARNING)
                i += 2  # skip the flag and its value
                continue
            i += 1  # a boolean flag (-i, -t, -it, --rm, --init, …); skip it
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


def _validate_docker_image(image: str) -> None:
    """Reject an image reference that could inject `docker run` options.

    ``build()`` emits the image as a positional after ``--`` (which neutralizes it), but a
    leading-dash image is invalid anyway and, on a legacy row without the ``--`` guard, would
    be parsed by docker as an extra option (``--volume=/:/host``, ``--privileged``, …) —
    bypassing every hardening flag. Reject it at the boundary so it can never persist."""
    if not image or image.startswith("-"):
        raise ValueError("a docker image reference can't be empty or start with '-'")


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
    """Drop env keys that ``_validate_docker_env`` would reject (malformed names, or the
    bridge's reserved DOCKER_*/PATH connection vars). Used by the boot migration, which
    can't raise on a legacy row the way create/update can."""
    return {
        k: v for k, v in env.items()
        if "=" not in k and not any(c.isspace() for c in k) and not is_reserved_docker_env(k)
    }


def normalize_docker_servers(session: Session) -> int:
    """Canonicalize legacy docker rows so enabling one is gated, hardened, and launches the
    right image.

    Two legacy shapes from a prior release (where the docker runner only raised):
    (1) ``runner="docker"`` rows stored verbatim (``command="docker"``, ``args=["run", …]``);
    (2) rows stored as ``runner="command"`` because the old ``_infer_runner`` matched only
        the literal ``"docker"`` — so ``command="/usr/local/bin/docker"`` imports slipped
        through as generic command servers that would bypass the docker gate + hardening.
    Both are re-normalized to the canonical (image, container_args, env) shape, converted to
    ``runner="docker"``, and have reserved/malformed env keys scrubbed. A row that's already
    canonical re-normalizes to itself (no write). Idempotent. Returns the count changed. A
    row that can't be parsed (no image) is left untouched — enabling it surfaces the error."""
    changed = 0
    for server in repo.list_servers(session):
        looks_docker_run = (
            _launcher_basename(server.command or "") in _DOCKER_LAUNCHERS
            and _docker_run_index(list(server.args or [])) is not None
        )
        if server.runner == "docker":
            pass  # always re-normalize existing docker rows (idempotent for canonical ones)
        elif server.runner == "command" and looks_docker_run:
            pass  # a docker `run …` invocation misclassified as a command runner — convert it
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
    enabled: bool = False,
    source: str = "manual",
) -> Server:
    if runner not in RUNNERS:
        raise ValueError(f"unknown runner {runner!r}; must be one of {RUNNERS}")
    if not command.strip():
        raise ValueError("command is required")
    # A `command` runner whose launcher is actually docker/podman IS the docker runner — route
    # it through the docker machinery (normalize + validate + gate + hardening + minimal_env)
    # so it can't launch containers ungated/unhardened with the full control-plane env.
    if runner == "command" and _launcher_basename(command) in _DOCKER_LAUNCHERS:
        runner = "docker"
    # A remote server reuses command/args for the upstream URL + transport; canonicalize
    # them up front so the persisted row (and config_hash) is deterministic. There is no
    # local process, so a working directory is meaningless — drop it.
    if runner == "remote":
        command, args = normalize_remote(command, args)
        cwd = None
    # A docker server is stored in canonical (image, container_args, env) shape; a pasted
    # `docker run …` invocation is parsed down to it. The gate bites only when the server
    # is created already enabled — a disabled import stays reviewable.
    if runner == "docker":
        command, args, env, warnings = normalize_docker(command, args, env)
        _validate_docker_image(command)
        _validate_docker_env(env)
        cwd = None  # a container has its own filesystem; a host cwd is meaningless
        for w in warnings:  # the hardened parser altered the invocation — don't do it silently
            logger.warning("docker server %r: %s", name, w)
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
}


def update_server(session: Session, server_id: str, changes: dict[str, Any]) -> Server:
    server = repo.get_server(session, server_id)
    if server is None:
        raise KeyError(server_id)
    # slug is identity/routing, not launch config: validated separately and excluded
    # from config_hash, so a rename re-routes the proxy without bouncing the bridge.
    if "slug" in changes:
        server.slug = _validate_slug(session, changes["slug"], current_id=server.id)
    for key, value in changes.items():
        if key in _MUTABLE_FIELDS:
            setattr(server, key, value)
    if server.runner not in RUNNERS:
        raise ValueError(f"unknown runner {server.runner!r}")
    # A `command` runner whose launcher is docker/podman IS the docker runner (see create) —
    # reclassify so it can't launch containers ungated/unhardened via passthrough.
    if server.runner == "command" and _launcher_basename(server.command) in _DOCKER_LAUNCHERS:
        server.runner = "docker"
    if server.runner == "remote":
        server.command, server.args = normalize_remote(server.command, server.args)
        # Converting a local server to remote: PATCH drops the form's cwd:null, so clear
        # the stale working directory here (remote has no process) to keep the row canonical.
        server.cwd = None
    if server.runner == "docker":
        server.command, server.args, server.env, warnings = normalize_docker(
            server.command, server.args, server.env
        )
        _validate_docker_image(server.command)
        _validate_docker_env(server.env)
        for w in warnings:
            logger.warning("docker server %r: %s", server.name, w)
        # Converting a local server (with a cwd) to docker: PATCH drops the form's cwd:null,
        # so clear the now-meaningless working directory here to keep the row canonical.
        server.cwd = None
        # NB: no enabled-gate here. An already-enabled docker server can be edited even while
        # the runner is off (so a broken image/env can be fixed) — the supervisor reconcile is
        # the gate that keeps it from actually running. Enabling is gated in set_enabled.
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
    if base in _DOCKER_LAUNCHERS:
        return "docker"
    return "command"


def import_mcp_servers(session: Session, data: dict) -> tuple[list[Server], list[dict]]:
    """Create servers from the standard Claude-Desktop ``mcpServers`` JSON shape.

    Accepts either ``{"mcpServers": {...}}`` or a bare ``{name: {...}}`` map.
    Stdio entries (``command`` + ``args`` + ``env``) become local servers; remote
    entries (``url`` / ``type: sse|streamable-http|http``) become ``remote`` servers
    that proxy the upstream URL. All are stored verbatim and disabled (the user
    reviews, then enables).
    """
    servers_map = data.get("mcpServers") if isinstance(data, dict) and "mcpServers" in data else data
    if not isinstance(servers_map, dict):
        raise ValueError("expected an object with an 'mcpServers' map of servers")

    created: list[Server] = []
    skipped: list[dict] = []
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
                )
            )
        except (ValueError, TypeError) as exc:
            skipped.append({"name": str(name), "reason": str(exc)})
    return created, skipped
