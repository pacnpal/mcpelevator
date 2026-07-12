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

Secrets are passed by NAME (``-e KEY``), never ``-e KEY=value``, so values never appear in
the container's argv, ``docker ps``, or ``docker inspect``. The values live in
``ProcessSpec.env`` and reach the docker CLI's own environment via the bridge host, which
for a docker spec passes a MINIMAL env (``minimal_env=True``) so ``-e KEY`` can only ever
resolve the operator-declared vars — never the control plane's own environment.

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

# The container name/label the supervisor's reaper keys off (see supervisor + unit).
LABEL_KEY = "mcpelevator.server"


def container_name(server: Server) -> str:
    """Deterministic, human-readable, collision-resistant container name.

    Same (slug, id) always yields the same name, so ``docker rm -f`` on stop targets the
    exact container this row launched. The id prefix keeps it unique even if two servers
    share a slug transiently (a rename racing a create)."""
    return f"mcpe-{server.slug}-{server.id[:8]}"


@register("docker")
def build(server: Server) -> ProcessSpec:
    env = dict(server.env or {})
    args = [
        *BASE_FLAGS,
        *HARDENING,
        "--memory", DEFAULT_MEMORY,
        "--name", container_name(server),
        "--label", f"{LABEL_KEY}={server.id}",
    ]
    # Name-only passthrough: the value is read from the docker CLI's environment (which the
    # bridge host seeds from ``env`` under a minimal allowlist), never embedded in argv.
    for key in env:
        args += ["-e", key]
    args += [server.command, *(server.args or [])]  # image ref, then the container's args
    return ProcessSpec(
        command=DOCKER_BIN,
        args=args,
        env=env,
        cwd=server.cwd,
        minimal_env=True,
    )
