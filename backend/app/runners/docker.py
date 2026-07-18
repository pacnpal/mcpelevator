"""docker runner — launch an MCP server packaged as a Docker (OCI) image.

The stored ``Server`` shape is canonical and minimal (SSOT): ``command`` is the image
reference, ``args`` are the *container's* own arguments, ``env`` is the env map, and
``run_args`` are optional operator-chosen ``docker run`` options placed before the image
(``--name``, ``--shm-size=1g``, …). This pure builder synthesizes the full hardened
``docker run …`` argv from that shape — exactly as the remote runner reinterprets its
stored fields. Because ``config_hash`` covers the stored shape and NOT this synthesized
argv, tweaking a hardening constant here never spuriously restarts every docker server;
and the same row always yields the same argv (Determinism).

Hardening (safe defaults, egress ON so servers like github-mcp-server can reach their
APIs): ``--rm`` (daemon-side auto-remove), ``--init`` (in-container signal handling /
zombie reaping), ``--cap-drop ALL``, ``--security-opt no-new-privileges``, a pids cap, a
generous memory cap, and a deterministic ``--name``/``--label`` the supervisor uses to
reap orphaned containers. Networking and the root filesystem are left at Docker's defaults
(egress allowed, rootfs writable) — operators tighten per server. ``run_args`` are emitted
AFTER the defaults, so a duplicated flag (``--memory 2g``) overrides them (docker is
last-wins) — loosening or tightening per server is a deliberate operator choice behind the
same root-equivalent gate.

Secrets are passed by NAME (``-e KEY``), never ``-e KEY=value``, so a value never appears in
mcpelevator's OWN process argv or ``ps`` output. (Docker still resolves the value into the
container's environment, which anyone with access to the Docker daemon can read via
``docker inspect`` — name-only passing narrows exposure to daemon-holders, it doesn't hide the
value from them.) The values live in ``ProcessSpec.env`` and reach the docker CLI's own
environment via the bridge host, which for a docker spec passes a MINIMAL env
(``minimal_env=True``) so ``-e KEY`` can only ever resolve the operator-declared vars — never
the control plane's own environment.

Enabling this runner is opt-in and root-equivalent (it runs arbitrary images on a Docker
daemon); the gate lives in the service/settings/supervisor layers, not here — this builder
is a pure ``Server -> ProcessSpec`` mapping with no I/O.
"""

from __future__ import annotations

from typing import Optional

from app.db.models import Server
from app.runners.base import ProcessSpec, register

# SSOT for the docker invocation. The builder is the single place that decides how a
# stored image+args+env becomes a launched container, so these constants define the
# entire security posture of a docker-run MCP server.
DOCKER_BIN = "docker"  # resolved on PATH; honors DOCKER_HOST at runtime (sibling vs dind)
BASE_FLAGS = ["run", "-i", "--rm", "--init"]
HARDENING = [
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "512",
]
DEFAULT_MEMORY = "1g"  # --memory; generous, but caps a runaway container from OOMing the host

# The label every launched container carries, valued with the server's id. It is the SOLE
# handle the supervisor/unit use to reap containers (`--filter label=mcpelevator.server=<id>`).
# We deliberately DON'T set a fixed `--name`: a pure builder can't make a name unique per
# launch, and FastMCP's fresh-session-per-request proxy can open more than one upstream for
# the same server (readiness probe + a client, or a reconnect overlap), which would collide
# on a fixed name. The label handles reaping without that constraint; Docker auto-names.
LABEL_KEY = "mcpelevator.server"

# Env vars the bridge controls for a docker child: its own executable resolution (PATH/HOME)
# and the docker daemon connection (DOCKER_*). SSOT, reused by the bridge (keeps these
# authoritative for the CLI) and the service layer (rejects them as *container* env — a
# `-e DOCKER_HOST` would otherwise leak the control daemon endpoint into an untrusted
# container, and passing PATH/HOME by name is meaningless).
DOCKER_ENV_ALLOWLIST = (
    "PATH", "HOME",
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONTEXT", "DOCKER_CONFIG",
)


def is_reserved_docker_env(key: str) -> bool:
    """True for an env name the docker CLI itself consumes, so a container must not set it.

    Covers the connection/exec allowlist (``PATH``/``HOME``/``DOCKER_HOST``…) AND every other
    ``DOCKER_*`` CLI variable (``DOCKER_API_VERSION``, ``DOCKER_DEFAULT_PLATFORM``,
    ``DOCKER_CUSTOM_HEADERS``, …). SSOT reused by the service (rejects/scrubs these as container
    env), the builder (defensive skip), and the bridge (keeps them out of the container).

    This is the NARROW set the bridge INHERITS from the operator's env into the CLI. The broader
    "a container must not set this" set is :func:`is_forbidden_container_env` (adds proxy vars)."""
    return key in DOCKER_ENV_ALLOWLIST or key.startswith("DOCKER_")


# Go proxy vars (either case) the docker CLI itself honors for ITS OWN HTTP requests. With a TCP
# ``DOCKER_HOST`` (a dind sidecar), a container-declared ``HTTP_PROXY`` etc. landing in the CLI's
# environment could reroute/break the control-plane's own daemon API call. So a container must not
# set them, and — unlike ``DOCKER_*`` — they are NOT inherited from the operator's env into the CLI
# either (that could break the CLI→dind connection). Proxy a container via the docker CLI's own
# ``proxies`` config instead (see docs/security.md).
_DOCKER_PROXY_ENV = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "FTP_PROXY"})


def is_forbidden_container_env(key: str) -> bool:
    """True for an env name a docker CONTAINER must not set because the docker CLI would consume
    it. Superset of :func:`is_reserved_docker_env` that also covers the Go proxy vars
    (case-insensitive). Used to REJECT (service), SKIP (builder ``-e``), and STRIP (bridge) these
    keys from anything a container config supplies."""
    return is_reserved_docker_env(key) or key.upper() in _DOCKER_PROXY_ENV


def server_label(server_id: str) -> str:
    """The `label=key=value` selector for a server's containers (SSOT for reaping)."""
    return f"{LABEL_KEY}={server_id}"


def _sets_reserved_label(value: str) -> bool:
    """True when a ``--label`` value (re)defines the reserved reaping key."""
    return value == LABEL_KEY or value.startswith(f"{LABEL_KEY}=")


def _forbidden_run_arg(tokens: list[str], i: int) -> Optional[tuple[str, int]]:
    """Classify ``tokens[i]`` as a forbidden ``run_args`` option: ``(reason,
    tokens_consumed)``, or ``None`` when it's allowed.

    SSOT for the (small) set of run options an operator may NOT supply, because each
    would break a guarantee the synthesized argv depends on — everything else is the
    operator's call behind the root-equivalent gate. The service uses the reason to
    reject at the boundary (:func:`run_args_error`); the builder uses the consumed
    count to defensively skip a flag *and its separated value* on a legacy/hand-edited
    row (:func:`sanitize_run_args`). Values of ALLOWED flags are scanned as ordinary
    tokens, so a value that spells a forbidden flag must use the inline ``--flag=value``
    form — a non-restriction in practice (values are names/sizes/durations)."""
    tok = tokens[i]
    if tok == "--":
        # Ends option parsing: docker would read the NEXT run_arg as the image, shifting
        # the real image into the container's argv. The builder owns the one real ``--``.
        return "'--' can't be a docker run option (the runner adds it before the image)", 1
    if tok in ("-d", "--detach") or tok.startswith("--detach="):
        return "-d/--detach can't be a docker run option — a stdio MCP server must stay attached", 1
    if (
        tok in ("-e", "--env")
        or tok.startswith("--env=")
        or (tok.startswith("-e") and not tok.startswith("--"))
    ):
        # Values in argv would leak into `ps` and break the name-only secret passing;
        # separated form consumes the NAME/NAME=value token too.
        return (
            "-e/--env can't be a docker run option — put container variables under the "
            "server's Environment (they're passed by name, never embedded in the command)",
            2 if tok in ("-e", "--env") else 1,
        )
    if tok == "--env-file" or tok.startswith("--env-file="):
        return (
            "--env-file can't be a docker run option — add its variables under the "
            "server's Environment instead",
            2 if tok == "--env-file" else 1,
        )
    # The reaping label is the SOLE handle the supervisor uses to find this server's
    # containers; overriding it could orphan them or spoof another server's reap set.
    reserved = f"the '{LABEL_KEY}' label is reserved for the docker runner (it marks containers to reap)"
    if tok in ("-l", "--label"):
        value = tokens[i + 1] if i + 1 < len(tokens) else ""
        if _sets_reserved_label(value):
            return reserved, 2
    elif tok.startswith("--label=") and _sets_reserved_label(tok.split("=", 1)[1]):
        return reserved, 1
    elif tok.startswith("-l") and not tok.startswith("--") and _sets_reserved_label(tok[2:]):
        return reserved, 1
    return None


def run_args_error(run_args: Optional[list]) -> Optional[str]:
    """Why an operator-supplied ``run_args`` list is unacceptable, or ``None`` if it's
    fine. Used by the registry service to reject at the create/update boundary so a
    forbidden option never persists."""
    tokens = list(run_args or [])
    for i, tok in enumerate(tokens):
        if not isinstance(tok, str) or not tok.strip():
            return "docker run options must be non-empty strings"
        hit = _forbidden_run_arg(tokens, i)
        if hit is not None:
            return hit[0]
    return None


def sanitize_run_args(run_args: Optional[list]) -> list[str]:
    """The stored ``run_args`` with any forbidden option (plus its separated value)
    dropped. The service already rejects these at the boundary; this guards a
    legacy/hand-edited row the same way the builder's ``-e`` loop guards env keys."""
    tokens = [t for t in (run_args or []) if isinstance(t, str) and t.strip()]
    out: list[str] = []
    i = 0
    while i < len(tokens):
        hit = _forbidden_run_arg(tokens, i)
        if hit is not None:
            i += hit[1]
            continue
        out.append(tokens[i])
        i += 1
    return out


@register("docker")
def build(server: Server) -> ProcessSpec:
    env = dict(server.env or {})
    args = [
        *BASE_FLAGS,
        *HARDENING,
        "--memory", DEFAULT_MEMORY,
        "--label", server_label(server.id),
    ]
    # Name-only passthrough: the value is read from the docker CLI's environment (which the
    # bridge host seeds from ``env`` under a minimal allowlist), never embedded in argv.
    # Defensively skip a malformed key (``=``/whitespace) or any reserved CLI key (the
    # PATH/HOME/DOCKER_* allowlist, or any other ``DOCKER_*`` CLI var) so a value can't enter
    # argv and a container can't receive/alter the CLI's own vars — the service layer already
    # rejects these, this guards a legacy/hand-edited row.
    for key in env:
        if "=" in key or any(c.isspace() for c in key) or is_forbidden_container_env(key):
            continue
        args += ["-e", key]
    # Operator-chosen run options (``--name``, ``--shm-size=1g``, …) go LAST among the
    # options so a duplicated flag overrides the defaults above (docker is last-wins).
    # The service rejects the forbidden ones at the boundary; sanitize_run_args guards
    # a legacy/hand-edited row.
    args += sanitize_run_args(server.run_args)
    # ``--`` terminates flag parsing: everything after it is positional (image, then the
    # container's args). Without it a ``command`` like ``--volume=/:/host`` or ``--privileged``
    # would be parsed by docker as an extra run OPTION (host mount / host namespace), bypassing
    # every hardening flag above. The service layer also rejects a leading-dash image, so this
    # is defense in depth for a legacy/hand-edited row.
    args += ["--", server.command, *(server.args or [])]  # image ref, then the container's args
    return ProcessSpec(
        command=DOCKER_BIN,
        args=args,
        env=env,
        # A container has its own filesystem — a host cwd is meaningless and a stale one
        # (e.g. from converting a command server) could break `docker run`. Never pass it.
        cwd=None,
        minimal_env=True,
    )
