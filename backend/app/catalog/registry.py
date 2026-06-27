"""The catalog source registry — the single source of truth for available directories.

To add a registry: write a module exposing a ``Source`` (see ``catalog.base.Source``)
and add one instance to ``_SOURCES`` below. Nothing else in the app enumerates sources —
the API serves this list at ``GET /api/catalog/sources`` and the SPA renders whatever it
returns, so a new registry shows up everywhere with no further wiring.
"""

from __future__ import annotations

from app.catalog.base import Source
from app.catalog.glama import GlamaSource
from app.catalog.official import OfficialSource

# Order is the display order in the browse UI; the first is the default source.
_SOURCES: list[Source] = [
    OfficialSource(),
    GlamaSource(),
]

SOURCES: dict[str, Source] = {s.id: s for s in _SOURCES}
DEFAULT_SOURCE: str = _SOURCES[0].id


def get_source(source_id: str) -> Source | None:
    """
    Get the catalog source registered for the given identifier.
    
    Parameters:
    	source_id (str): The source identifier.
    
    Returns:
    	Source | None: The matching source, or None if no source is registered for the identifier.
    """
    return SOURCES.get(source_id)


def source_list() -> list[dict[str, str]]:
    """
    Return the catalog source descriptors used by the browse view.
    
    Returns:
        list[dict[str, str]]: A list of source descriptors containing each source's
        ``id``, ``label``, and ``install_support`` values.
    """
    return [
        {"id": s.id, "label": s.label, "install_support": s.install_support}
        for s in _SOURCES
    ]
