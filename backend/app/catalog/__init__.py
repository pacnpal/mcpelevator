"""MCP catalog — browse upstream MCP directories and install servers.

A small plugin architecture: each upstream directory is a ``Source`` (``base.Source``)
in its own module (``official``, ``glama``, …), registered once in ``registry`` (the
SSOT). The shared, pure ``mapping`` core turns a registry *package* into an mcpelevator
launch spec the existing ``ServerForm`` / ``POST /api/servers`` flow consumes. mcpelevator
stays a control plane — we never persist registry data, just resolve it on demand into a
reviewable, deterministic draft.

Adding a registry = one module implementing ``Source`` + one line in ``registry``.
"""

from app.catalog import base, mapping, registry
from app.catalog.base import CatalogUpstreamError, Source

__all__ = ["base", "mapping", "registry", "CatalogUpstreamError", "Source"]
