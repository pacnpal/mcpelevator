"""Catalog source seam — the contract every registry plugs into.

Adding a registry is one self-contained module: implement ``Source`` (an ``id``,
``label``, ``install_support``, and the two async fetchers) and register it in
``catalog.registry`` (the SSOT list). The API layer and the SPA never special-case a
source — they iterate the registry and speak the normalized shapes in ``api.schemas``.

Shared, side-effect-free-ish infrastructure lives here: a fail-fast JSON GET (the
shared ``app.state.http`` client has ``timeout=None`` for SSE proxying, wrong for a
directory fetch), a small TTL cache, and the upstream-error wrapper that the API maps
to a clean 502.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol

import httpx

_TIMEOUT = httpx.Timeout(15.0)
DEFAULT_LIMIT = 50
MAX_LIMIT = 100


class CatalogUpstreamError(Exception):
    """An upstream directory was unreachable or returned an unusable response."""


def clamp_limit(limit: int | None) -> int:
    """
    Normalize a requested page size to the supported range.
    
    Parameters:
    	limit (int | None): Requested page size.
    
    Returns:
    	int: The normalized page size, using the default value when the request is missing or less than 1, and capped at the maximum limit otherwise.
    """
    if not limit or limit < 1:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


async def get_json(http: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
    """
    Fetch JSON from an upstream endpoint.
    
    Parameters:
    	http (httpx.AsyncClient): HTTP client used for the request.
    	url (str): Request URL.
    	params (dict[str, Any]): Query parameters to include after removing entries with `None` or empty-string values.
    
    Returns:
    	Any: The decoded JSON response.
    
    Raises:
    	CatalogUpstreamError: If the request fails, the response is not usable, or the upstream returns a non-404 HTTP error.
    	httpx.HTTPStatusError: If the upstream returns a 404 response.
    """
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    try:
        resp = await http.get(url, params=clean, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise  # let the source/API map 404 → 404
        raise CatalogUpstreamError(f"upstream returned {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise CatalogUpstreamError(str(exc) or "upstream request failed") from exc


class TTLCache:
    """Tiny monotonic-clock TTL cache. Spares upstreams from repeated identical list
    queries (the browse view re-queries on every debounced keystroke)."""

    def __init__(self, ttl: float = 300.0, max_entries: int = 1000) -> None:
        """
        Initialize the cache with a time-to-live and a max-entries cap.

        Parameters:
        	ttl (float): Cache lifetime in seconds.
        	max_entries (int): Size past which expired entries are pruned on insert.
        """
        self._ttl = ttl
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """
        Get a cached value if it is still valid.
        
        Parameters:
        	key (str): Cache key.
        
        Returns:
        	The cached value if present and not expired, or None.
        """
        hit = self._store.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        """
        Store a value in the cache with a fresh expiration time.

        Parameters:
        	key (str): Cache key.
        	value (Any): Value to store.
        """
        # `get` only evicts the keys it touches, so a stream of unique search queries
        # (keyed by user-controlled search/cursor) would otherwise grow _store forever.
        # At the cap, drop expired entries first, then — if still full (all live) — the
        # soonest-to-expire one, so the cap is a hard bound, not just expiry-bounded.
        if len(self._store) >= self._max_entries:
            now = time.monotonic()
            self._store = {k: (exp, v) for k, (exp, v) in self._store.items() if exp > now}
            # Only evict for a genuine insert; refreshing an existing live key must not
            # shrink the cache (that would evict a hot entry and cause avoidable fetches).
            if key not in self._store and len(self._store) >= self._max_entries:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                self._store.pop(oldest, None)
        self._store[key] = (time.monotonic() + self._ttl, value)


class Source(Protocol):
    """One upstream MCP directory.

    Implementations normalize to the ``api.schemas`` shapes:
    ``list_servers`` → ``{"servers": [CatalogServer...], "next_cursor": str | None}``;
    ``get_detail`` → a ``CatalogDetail`` dict. Both should raise ``CatalogUpstreamError``
    on a bad upstream and let a 404 propagate as ``httpx.HTTPStatusError``.
    """

    id: str
    label: str
    # "auto" (a runnable command is derivable) | "manual" (discovery only). Same literal
    # domain as the CatalogSource API contract, so sources are type-checked against it.
    install_support: Literal["auto", "manual"]

    async def list_servers(
        self, http: httpx.AsyncClient, *, search: str | None, cursor: str | None, limit: int | None
    ) -> dict[str, Any]:
        """
        List servers from the source.

        Parameters:
        	search (str | None): Search text used to filter matching servers.
        	cursor (str | None): Pagination cursor for the next page of results.
        	limit (int | None): Maximum number of servers to return.

        Returns:
        	dict[str, Any]: A normalized listing with server entries and a next-page cursor.
        """
        ...

    async def get_detail(
        self, http: httpx.AsyncClient, *, id: str, version: str
    ) -> dict[str, Any]:
        """
        Fetch a normalized catalog entry detail for a specific server version.

        Parameters:
        	id (str): The server identifier.
        	version (str): The server version to retrieve.

        Returns:
        	dict[str, Any]: A normalized catalog detail mapping.
        """
        ...

    async def list_versions(self, http: httpx.AsyncClient, *, id: str) -> list[str]:
        """
        List a server's available versions, latest first.

        Returns an empty list for sources without a version concept (e.g. discovery-only
        directories). Raises ``CatalogUpstreamError`` on a bad upstream.
        """
        ...
