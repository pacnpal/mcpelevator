"""Group endpoints: named bundles of registered servers served at ``/g/<name>/mcp``.

The group registry (``registry``) is the single source of truth for what ``/g``
serves; the hub (``hub``) owns one FastMCP proxy bundle per group; the route
(``route``) dispatches ``/g/<name>/...`` requests through the same auth chokepoint
as the per-server proxy.
"""
