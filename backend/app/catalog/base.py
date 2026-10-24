"""Catalog source seam — the contract every registry plugs into.

Adding a registry is one self-contained module: implement ``Source`` (an ``id``,
``label``, ``install_support``, and the two async fetchers) and register it in
``catalog.registry`` (the SSOT list). The API layer and the SPA never special-case a
source — they iterate the registry and speak the normalized shapes in ``api.schemas``.

Shared, side-effect-free-ish infrastructure lives here: a fail-fast JSON GET (the
shared ``app.state.http`` client has ``timeout=None`` for SSE proxying, wrong for a
directory fetch), a small TTL cache that can serve its last good value when the
upstream is down (``cached``), and the upstream-error wrapper that the API maps to a
clean 503 (not 502: Cloudflare replaces an origin 502/504 with its own error page,
hiding the detail).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, NamedTuple, Protocol, TypeVar

import httpx

_TIMEOUT = httpx.Timeout(15.0)
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

logger = logging.getLogger(__name__)


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


async def get_json(
    http: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Any:
    """
    Fetch JSON from an upstream endpoint.
    
    Parameters:
    	http (httpx.AsyncClient): HTTP client used for the request.
    	url (str): Request URL.
    	params (dict[str, Any]): Query parameters to include after removing entries with `None` or empty-string values.
    	headers (dict[str, str] | None): Extra request headers (e.g. an upstream API key).
    
    Returns:
    	Any: The decoded JSON response.
    
    Raises:
    	CatalogUpstreamError: If the request fails, the response is not usable, or the upstream returns a non-404 HTTP error.
    	httpx.HTTPStatusError: If the upstream returns a 404 response.
    """
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    try:
        resp = await http.get(url, params=clean, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise  # let the source/API map 404 → 404
        logger.warning("catalog upstream %s returned %s", url, exc.response.status_code)
        raise CatalogUpstreamError(f"upstream returned {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        # httpx timeouts often stringify empty; the type (ConnectTimeout vs ReadTimeout vs
        # ConnectError) is what tells an operator whether egress, DNS or the upstream is at fault.
        reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        logger.warning("catalog upstream %s failed: %s", url, reason)
        raise CatalogUpstreamError(reason) from exc


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
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]

    def stale(self, key: str) -> Any | None:
        """The last value stored under ``key``, expired or not. An expired entry stays
        until ``put`` prunes it at the size cap, so it can stand in for an upstream
        that is down."""
        hit = self._store.get(key)
        return None if hit is None else hit[1]

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


T = TypeVar("T")


async def cached(cache: TTLCache, key: str, fetch: Callable[[], Awaitable[T]]) -> T:
    """Return a fresh cache hit, else fetch and cache. If the fetch fails upstream and
    an expired value is still held, serve that instead of the error: a directory
    listing an hour old beats a catalog page that won't load."""
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        value = await fetch()
    except CatalogUpstreamError:
        stale = cache.stale(key)
        if stale is None:
            raise
        logger.warning("catalog upstream down, serving stale %s", key)
        return stale
    cache.put(key, value)
    return value


class MirrorRow(NamedTuple):
    """One upstream record as the local mirror stores it (see ``catalog.mirror``)."""

    id: str  # the same key ``get_detail`` takes
    status: str  # "deleted" rows are kept (so a status change lands) but never listed
    text: str  # what local search matches against
    raw: dict[str, Any]  # the upstream document, re-normalized on read


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
    # A license-required credit link, or None (see CatalogSource.attribution_url).
    attribution_url: str | None
    # True when ``mirror_pages`` honors ``since``; False means each sync is a full pass.
    mirror_incremental: bool

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

    def enabled(self) -> bool:
        """Whether this source is offered at all (e.g. False until its API key is set).
        A disabled source is absent from the source list, the API and the mirror."""
        ...

    def mirror_interval(self) -> float | None:
        """Seconds between mirror syncs, or None to not mirror this source."""
        ...

    def mirror_pages(
        self, http: httpx.AsyncClient, *, since: str | None
    ) -> AsyncIterator[list[MirrorRow]]:
        """Yield the whole directory a page at a time, for ``catalog.mirror``.

        ``since`` (an RFC 3339 timestamp) asks for only what changed after it; a source
        that can't filter that way ignores it and returns everything, and says so with
        ``mirror_incremental = False``.
        """
        ...
