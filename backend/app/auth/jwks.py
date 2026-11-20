"""A cache of the signing keys published at a JWKS URL, for any JWT verifier.

Lookups on the event loop never wait on the network: they answer from the last good
keys while a refresh runs in a thread, one at a time per URL. Off the loop the fetch
runs in place. An unknown kid refetches at most once per window, a failed fetch backs
off, and keys older than a day stop answering.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from functools import lru_cache
from typing import Any

import jwt

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _keys(url: str) -> jwt.PyJWKClient:
    # PyJWKClient fetches synchronously. On the event loop that would stall every
    # request, so a refresh there runs in a thread (_refresh_soon) while the last
    # good keys keep answering; see _fetch.
    return jwt.PyJWKClient(url, lifespan=3600, timeout=5)


# An unknown kid refetches the keys (issuers rotate them), but no more often than
# this: otherwise every junk token would cost a blocking outbound request.
_REFETCH_S = 300.0
_last_refetch: dict[str, float] = {}
# After a failed fetch (the endpoint down or slow), refuse for this long instead of
# letting every queued token block on another doomed fetch.
_FAILED_BACKOFF_S = 30.0
_last_failure: dict[str, float] = {}
# The last keys each URL answered with, and when. They keep sign-ins working on the
# event loop while a refresh runs, but no longer than this: a key the issuer stopped
# publishing a day ago is no longer trusted just because its endpoint is down.
_STALE_S = 86400.0
_good: dict[str, tuple[dict[str, Any], float]] = {}
_refreshing: set[str] = set()
_refreshing_lock = threading.Lock()


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _download(url: str, *, refresh: bool) -> dict[str, Any]:
    try:
        keys = {k.key_id: k.key for k in _keys(url).get_signing_keys(refresh=refresh)}
    except (jwt.PyJWKClientError, jwt.PyJWKSetError, ValueError) as exc:
        # Unreachable, or answered with something that isn't a key set (bad JSON,
        # no keys): either way back off, and refuse the sign-in rather than fail it.
        _last_failure[url] = time.monotonic()
        if isinstance(exc, jwt.PyJWKClientConnectionError):
            raise
        raise jwt.PyJWKClientConnectionError(f"the signing keys at {url} couldn't be read: {exc}") from exc
    _good[url] = (keys, time.monotonic())
    if refresh:
        # Only a refetch that worked starts the unknown-kid window: one that failed is
        # covered by the shorter failure backoff, so a key rotated during an outage is
        # picked up as soon as the issuer answers again.
        _last_refetch[url] = time.monotonic()
    return keys


def _refresh_soon(url: str, *, refresh: bool) -> None:
    """Fetch the URL's keys in a thread, once at a time."""
    with _refreshing_lock:
        if url in _refreshing:
            return
        _refreshing.add(url)

    def run() -> None:
        try:
            _download(url, refresh=refresh)
        except jwt.PyJWKClientConnectionError as exc:
            logger.info("Signing key refresh failed: %s", exc)
        finally:
            with _refreshing_lock:
                _refreshing.discard(url)

    threading.Thread(target=run, name="jwks-keys", daemon=True).start()


def prewarm(url: str) -> None:
    """Load the URL's keys in the background, so the first lookup has them."""
    _refresh_soon(url, refresh=False)


def _fetch(url: str, *, refresh: bool) -> dict[str, Any]:
    client = _keys(url)
    if not refresh and client.jwk_set_cache is not None and client.jwk_set_cache.get() is not None:
        # Still-fresh keys never wait on the backoff: a failed refetch for one junk
        # kid must not lock out every valid sign-in. A cache hit makes no request.
        try:
            return {k.key_id: k.key for k in client.get_signing_keys()}
        except (jwt.PyJWKClientError, jwt.PyJWKSetError):
            # A cached set with no usable keys ({"keys": []}) would fail every
            # sign-in until it expires: refetch it instead, under the backoff.
            refresh = True
    backing_off = time.monotonic() - _last_failure.get(url, -_FAILED_BACKOFF_S) < _FAILED_BACKOFF_S
    if _on_loop():
        # A fetch here would stall every request on the loop: refresh in a thread and
        # answer from the last keys. A kid they lack, or no keys yet (the startup
        # load still running), is refused until the refresh lands.
        if not backing_off:
            _refresh_soon(url, refresh=refresh)
        last = _good.get(url)
        if last is not None and time.monotonic() - last[1] < _STALE_S:
            return last[0]
        raise jwt.PyJWKClientConnectionError(f"the signing keys at {url} are still loading")
    if backing_off:
        raise jwt.PyJWKClientConnectionError(f"the signing keys at {url} were unreachable moments ago")
    return _download(url, refresh=refresh)


def signing_key(url: str, token: str) -> Any:
    """The key, published at ``url``, that ``token``'s header names.

    A token with no kid takes the set's only key, as the ``oauth`` verifier does, and
    is refused when the set holds several. Raises ``jwt.PyJWKClientConnectionError``
    when the keys can't be had right now and ``jwt.InvalidTokenError`` when they don't
    include the token's kid."""
    kid = jwt.get_unverified_header(token).get("kid")
    if kid is None:
        keys = _fetch(url, refresh=False)
        if len(keys) != 1:
            raise jwt.InvalidTokenError("the token names no key and the issuer publishes several")
        return next(iter(keys.values()))
    if not isinstance(kid, str):  # a list or object would fail the lookup below
        raise jwt.InvalidTokenError("the token names no key")
    keys = _fetch(url, refresh=False)
    if kid not in keys and time.monotonic() - _last_refetch.get(url, -_REFETCH_S) >= _REFETCH_S:
        keys = _fetch(url, refresh=True)
    if kid not in keys:
        raise jwt.InvalidTokenError("signed with a key the issuer doesn't publish")
    return keys[kid]
