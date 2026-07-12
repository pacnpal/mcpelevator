"""docker runner — launch an MCP server packaged as a Docker (OCI) image.

The stored ``Server`` shape is canonical and minimal (SSOT): ``command`` is the image
reference, ``args`` are the *container's* own arguments, and ``env`` is the env map. This
pure builder synthesizes the full hardened ``docker run …`` argv from that shape — exactly
as the remote runner reinterprets its stored fields. Because ``config_hash`` covers the
stored shape and NOT this synthesized argv, tweaking a hardening constant here never
spuriously restarts every docker server; and the same row always yields the same argv
(Determinism).

Hardening (safe defaults, egress ON so servers like github-mcp-server can reach their
APIs): ``--rm`` (daemon-side auto-remove), ``--init`` (in-container signal handling /
zombie reaping), ``--cap-drop ALL``, ``--security-opt no-new-privileges``, a pids cap, a
generous memory cap, and a deterministic ``--name``/``--label`` the supervisor uses to
reap orphaned containers. Networking and the root filesystem are left at Docker's defaults
(egress allowed, rootfs writable) — operators tighten per server.

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
    env), the builder (defensive skip), and the bridge (keeps them out of the container)."""
    return key in DOCKER_ENV_ALLOWLIST or key.startswith("DOCKER_")


def server_label(server_id: str) -> str:
    """The `label=key=value` selector for a server's containers (SSOT for reaping)."""
    return f"{LABEL_KEY}={server_id}"


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
        if "=" in key or any(c.isspace() for c in key) or is_reserved_docker_env(key):
            continue
        args += ["-e", key]
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
