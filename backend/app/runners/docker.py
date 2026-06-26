"""docker runner — launch an MCP server packaged as a Docker image.

Hardening (read-only rootfs, dropped caps, no host net, resource limits) and the
opt-in gate are added in M7. Until then this raises, so the seam exists but the
dangerous path is closed by default.
"""

from __future__ import annotations

from app.db.models import Server
from app.runners.base import ProcessSpec, register


@register("docker")
def build(server: Server) -> ProcessSpec:
    raise NotImplementedError("docker runner is enabled in milestone M7")
