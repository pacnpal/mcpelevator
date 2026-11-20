"""The single OpenID Connect provider this instance signs users in with.

This module talks to the identity provider. Settings live in ``app.registry.settings``
(the ``oidc_*`` keys); WHO a signed-in user is stays in ``app.auth.principal``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.registry import settings as runtime_settings

logger = logging.getLogger(__name__)

# Every request to the identity provider goes through this transport. Tests swap in an
# ``httpx.MockTransport`` as a fake provider; None is httpx's real network transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

_TIMEOUT_S = 5.0
_REQUIRED = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")


def client() -> httpx.AsyncClient:
    """An HTTP client for the identity provider (no redirects, short timeout)."""
    return httpx.AsyncClient(transport=TRANSPORT, timeout=_TIMEOUT_S, follow_redirects=False)


async def check_settings(current: dict[str, Any], changes: dict[str, Any]) -> None:
    """Before a settings save: when it leaves SSO enabled and touches an ``oidc_*`` key,
    fetch the discovery document it would use. Raises ``ValueError`` naming the problem."""
    if not any(key.startswith("oidc_") for key in changes):
        return
    if not changes.get("oidc_enabled", current["oidc_enabled"]):
        return
    url = changes.get("oidc_discovery_url", current["oidc_discovery_url"])
    if not isinstance(url, str) or not url.strip():
        raise ValueError("set oidc_discovery_url before enabling SSO")
    await fetch_discovery(url.strip())


async def fetch_discovery(discovery_url: str) -> dict[str, Any]:
    """Fetch and check the provider's discovery document. ``discovery_url`` may be a bare
    issuer. Raises ``ValueError`` naming the problem."""
    url = runtime_settings.normalize_config_url(discovery_url)
    if not runtime_settings.is_valid_oauth_endpoint_url(url):
        raise ValueError(f"invalid oidc_discovery_url: {discovery_url!r}")
    try:
        async with client() as http:
            resp = await http.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("oidc discovery fetch from %s failed: %s", url, exc)
        raise ValueError(f"couldn't fetch the OIDC discovery document from {url}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"the OIDC discovery document at {url} is not a JSON object")
    bad = [k for k in _REQUIRED if not runtime_settings.is_valid_oauth_endpoint_url(doc.get(k))]
    if bad:
        raise ValueError(
            f"the OIDC discovery document at {url} lacks a valid {', '.join(bad)}"
        )
    return doc
