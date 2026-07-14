"""Unified MCP endpoint — one aggregated Streamable-HTTP surface at ``/s/all/mcp``.

Opt-in (the ``unified_endpoint`` setting). The hub mounts a FastMCP proxy per
running server (all of them, or the operator-selected subset in ``unified_servers``)
into a single FastMCP instance, namespaced by slug, and serves it in the
control-plane process. See ``hub.py`` (lifecycle) and ``route.py`` (ASGI dispatch).
"""
