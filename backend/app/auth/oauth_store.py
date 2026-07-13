"""Persistent OAuth token storage for remote (HTTP/SSE) upstreams.

A remote MCP server may require OAuth instead of a static bearer/API-key header.
The OAuth handshake (Dynamic Client Registration + authorization-code grant with
PKCE) is interactive — it needs a browser — so it runs in the *control plane*
(see ``app.auth.oauth_flow``). But the *bridge* subprocess is what actually makes
the upstream MCP calls and therefore needs the resulting tokens (and must be able
to refresh them). The two processes share the credentials through this file store.

One JSON file per server lives under ``<data_dir>/oauth/<server_id>.json`` and
holds three things:

* ``tokens`` — the current ``OAuthToken`` (access + refresh + expiry).
* ``client_info`` — the DCR (or static) client registration, so refresh works.
* ``metadata`` — the discovered ``OAuthMetadata`` (token endpoint, etc.), so the
  bridge can refresh *without* re-running discovery. Without a stored token
  endpoint the SDK's provider falls back to ``<server-url>/token`` (the MCP host,
  not the auth server) and refresh would target the wrong URL.

The store implements the MCP SDK's ``TokenStorage`` protocol (the four async
get/set methods the ``OAuthClientProvider`` calls), plus small sync helpers the
API uses to report auth status. Writes are atomic (temp file + ``os.replace``)
and the file is created ``0600`` — it holds bearer credentials.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from mcp.client.auth import TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)

from app.config import get_settings


def oauth_dir() -> Path:
    """The directory holding per-server OAuth credential files.

    Resolved from ``data_dir`` (absolute) so the control plane and each bridge
    subprocess — which share the same ``MCPE_*`` environment but may differ in
    incidental cwd — always agree on the path."""
    return get_settings().data_dir.resolve() / "oauth"


def token_path(server_id: str) -> Path:
    return oauth_dir() / f"{server_id}.json"


class ServerTokenStorage(TokenStorage):
    """File-backed ``TokenStorage`` for one server's upstream OAuth credentials.

    The same class is used by the control plane (which writes tokens after the
    interactive grant) and by the bridge (which reads them and writes back on
    refresh). It is deliberately tiny and synchronous under the hood: the files
    are a few KB and read/written rarely, so a plain JSON round-trip per call is
    simpler and safer than pulling in an async KV backend.
    """

    def __init__(self, server_id: str):
        self.server_id = server_id
        self.path = token_path(server_id)

    # --- raw file I/O ---------------------------------------------------- #

    def _read(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        # 0600 from creation — this file holds bearer + refresh tokens.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)

    # --- TokenStorage protocol ------------------------------------------- #

    async def get_tokens(self) -> Optional[OAuthToken]:
        raw = self._read().get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except ValueError:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # Persist an absolute expiry so a fresh process doesn't misread the
        # relative ``expires_in`` as "seconds from now" on every reload.
        if tokens.expires_in is not None:
            data["expires_at"] = time.time() + int(tokens.expires_in)
        else:
            data.pop("expires_at", None)
        self._write(data)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        raw = self._read().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValueError:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)

    # --- metadata (extra; not part of the protocol) ---------------------- #

    def get_token_expiry(self) -> Optional[float]:
        """The stored absolute access-token expiry (unix seconds), or ``None``.

        The bridge preloads this onto the provider so ``is_token_valid()`` is accurate
        on the first request: without it the SDK treats an unknown expiry as *valid*
        and, when the token has actually lapsed, an upstream 401 drives the full
        (interactive) re-auth path instead of a silent refresh."""
        val = self._read().get("expires_at")
        return float(val) if isinstance(val, (int, float)) else None

    def get_metadata(self) -> Optional[OAuthMetadata]:
        raw = self._read().get("metadata")
        if not raw:
            return None
        try:
            return OAuthMetadata.model_validate(raw)
        except ValueError:
            return None

    def set_metadata(self, metadata: OAuthMetadata) -> None:
        data = self._read()
        data["metadata"] = metadata.model_dump(mode="json", exclude_none=True)
        self._write(data)

    # --- sync status helpers (used by the API) --------------------------- #

    def status(self) -> dict:
        """A snapshot for the UI: whether tokens exist, when they expire, and
        whether a refresh token is present (so the operator knows re-auth is
        only needed once the refresh token itself lapses)."""
        data = self._read()
        tokens = data.get("tokens") or {}
        has_tokens = bool(tokens.get("access_token"))
        expires_at = data.get("expires_at")
        return {
            "authenticated": has_tokens,
            "expires_at": float(expires_at) if isinstance(expires_at, (int, float)) else None,
            "has_refresh_token": bool(tokens.get("refresh_token")),
        }

    def clear(self) -> None:
        """Delete the credential file (e.g. when the server is removed or the
        operator disconnects the upstream). Idempotent."""
        self.path.unlink(missing_ok=True)
