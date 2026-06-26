"""Runner registry. Importing the package registers every built-in builder."""

from __future__ import annotations

from app.runners.base import ProcessSpec, build_spec  # noqa: F401

# Side-effect imports: each module registers its builder on import.
from app.runners import command, docker, npx, uvx  # noqa: E402,F401
