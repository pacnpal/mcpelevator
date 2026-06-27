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
    return SOURCES.get(source_id)


def source_list() -> list[dict[str, str]]:
    """The browse-view source descriptors (id/label/install_support)."""
    return [
        {"id": s.id, "label": s.label, "install_support": s.install_support}
        for s in _SOURCES
    ]
