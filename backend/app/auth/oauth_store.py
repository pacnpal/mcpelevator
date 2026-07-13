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

import contextlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX-only; absent on Windows.
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

from mcp.client.auth import TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

# Server ids are opaque ``new_id()`` hex tokens; constrain hard so a value that ever
# reached here from a request path (``/servers/{server_id}/oauth/...``) can never
# traverse out of the oauth directory. A non-matching id is refused, not sanitized.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

# When a token response omits ``expires_in`` (RFC 6749 §5.1 makes it optional) BUT a refresh
# token is present, assume a modest lifetime so the bridge takes the proactive-refresh path.
# Without an expiry the SDK treats the token as valid forever, so a token that actually lapses
# is sent stale, 401s, and drops into the interactive path (which the bridge can't run). Only
# applied when refreshable — a genuinely long-lived, non-refreshable token must keep being sent.
_DEFAULT_REFRESHABLE_TTL = 3600


def _expires_at_for(tokens: "OAuthToken", *, has_refresh_token: bool) -> Optional[float]:
    if tokens.expires_in is not None:
        return time.time() + int(tokens.expires_in)
    if has_refresh_token:
        return time.time() + _DEFAULT_REFRESHABLE_TTL
    return None


def oauth_dir() -> Path:
    """The directory holding per-server OAuth credential files.

    Resolved from ``data_dir`` (absolute) so the control plane and each bridge
    subprocess — which share the same ``MCPE_*`` environment but may differ in
    incidental cwd — always agree on the path."""
    return get_settings().data_dir.resolve() / "oauth"


def token_path(server_id: str) -> Path:
    """Path to a server's credential file, refusing any id that isn't a plain token.

    ``server_id`` can originate from a request path parameter, so validate it before
    it touches the filesystem: only ``[A-Za-z0-9_-]`` is allowed, which cannot contain
    a path separator or ``..`` and therefore cannot escape :func:`oauth_dir`."""
    if not _SAFE_ID.fullmatch(server_id or ""):
        raise ValueError(f"invalid server id {server_id!r}")
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

    @contextlib.contextmanager
    def _locked(self):
        """Advisory inter-process lock over a read-modify-write.

        The control plane (interactive auth) and the bridge (token refresh) both mutate
        this file; without a lock two interleaved read→mutate→replace cycles would
        last-writer-win and could drop a just-refreshed token. An ``flock`` on a sidecar
        ``.lock`` file serializes the whole RMW across processes; it's released on every
        path. The ``_write`` temp file is per-call regardless, so an atomic replace is
        never torn even if a reader isn't holding the lock.

        Where ``fcntl`` is unavailable (Windows) we skip the advisory lock and lean on the
        atomic ``os.replace`` alone: writes still can't tear, we only lose cross-process
        RMW serialization (a rarely-hit refresh/auth race), and — importantly — the module
        still imports so the rest of the backend runs."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A UNIQUE temp file per write (not a shared ``<id>.json.tmp``): two concurrent
        # writers must not share the same temp path, or one ``os.replace`` moves the file
        # the other still expects. ``mkstemp`` also creates it 0600 — this holds tokens.
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=f"{self.server_id}.", suffix=".tmp")
        tmp = Path(tmp_name)
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
        with self._locked():
            data = self._read()
            # A refresh response frequently OMITS ``refresh_token`` (the provider isn't
            # rotating it). Carry the previous one forward so the next refresh still has a
            # credential — otherwise the token silently becomes single-use and the bridge
            # falls into the non-interactive failure path at the next expiry. MUTATE the
            # token object (not just the serialized dict) so the SDK provider's in-memory
            # ``context.current_tokens`` — the same object it passes here — stays consistent.
            if not tokens.refresh_token:
                prev = (data.get("tokens") or {}).get("refresh_token")
                if prev:
                    try:
                        tokens.refresh_token = prev
                    except Exception:  # noqa: BLE001 — frozen model: fall back to fixing the JSON only
                        logger.debug(
                            "could not set refresh_token on the token object; patching JSON only",
                            exc_info=True,
                        )
            dumped = tokens.model_dump(mode="json", exclude_none=True)
            if not dumped.get("refresh_token"):
                prev = (data.get("tokens") or {}).get("refresh_token")
                if prev:
                    dumped["refresh_token"] = prev
            data["tokens"] = dumped
            # Persist an absolute expiry so a fresh process doesn't misread the
            # relative ``expires_in`` as "seconds from now" on every reload. When the
            # provider omits ``expires_in`` but we (still) hold a refresh token, stamp a
            # default TTL so the bridge proactively refreshes instead of sending a token
            # it wrongly believes is eternal (see ``_expires_at_for``).
            expires_at = _expires_at_for(
                tokens, has_refresh_token=bool(dumped.get("refresh_token"))
            )
            if expires_at is not None:
                data["expires_at"] = expires_at
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
        with self._locked():
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
        with self._locked():
            data = self._read()
            data["metadata"] = metadata.model_dump(mode="json", exclude_none=True)
            self._write(data)

    def get_protected_resource_metadata(self) -> Optional[ProtectedResourceMetadata]:
        raw = self._read().get("protected_resource_metadata")
        if not raw:
            return None
        try:
            return ProtectedResourceMetadata.model_validate(raw)
        except ValueError:
            return None

    def set_protected_resource_metadata(self, prm: ProtectedResourceMetadata) -> None:
        # The auth may bind tokens to the PRM ``resource`` (a parent of the MCP URL); the
        # bridge must refresh against that SAME resource, so persist the PRM and preload it
        # (see host._build_oauth_auth) rather than letting the SDK recompute it from the URL.
        with self._locked():
            data = self._read()
            data["protected_resource_metadata"] = prm.model_dump(mode="json", exclude_none=True)
            self._write(data)

    def promote(
        self,
        *,
        tokens: OAuthToken,
        client_info: Optional[OAuthClientInformationFull] = None,
        metadata: Optional[OAuthMetadata] = None,
        protected_resource_metadata: Optional[ProtectedResourceMetadata] = None,
    ) -> None:
        """Atomically install a freshly-obtained interactive grant, fully REPLACING any
        prior credentials in a SINGLE locked write.

        Used by the control-plane flow to commit a completed sign-in. Because it's one
        write built entirely in memory first, a failure (e.g. a bad model_dump) leaves the
        existing file — and thus a still-working credential — untouched: no destructive
        pre-clear. It also does NOT carry forward a previous refresh token (that's only for
        the bridge's refresh path); a new grant brings its own, so grafting the old one
        could bind the wrong account.

        Discovery artifacts (client_info / metadata / protected_resource_metadata) ARE
        carried forward when the caller passes ``None`` for one: those aren't credentials,
        and a grant that skipped a discovery step (e.g. an upstream that advertises AS
        metadata but no PRM) shouldn't blank out something a prior sign-in learned — the
        bridge needs the token endpoint / resource to refresh."""
        with self._locked():
            existing = self._read()
            data: dict = {}
            new_client_info = (
                client_info.model_dump(mode="json", exclude_none=True)
                if client_info is not None
                else existing.get("client_info")
            )
            if new_client_info:
                data["client_info"] = new_client_info
            new_metadata = (
                metadata.model_dump(mode="json", exclude_none=True)
                if metadata is not None
                else existing.get("metadata")
            )
            if new_metadata:
                data["metadata"] = new_metadata
            new_prm = (
                protected_resource_metadata.model_dump(mode="json", exclude_none=True)
                if protected_resource_metadata is not None
                else existing.get("protected_resource_metadata")
            )
            if new_prm:
                data["protected_resource_metadata"] = new_prm
            data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            expires_at = _expires_at_for(
                tokens, has_refresh_token=bool(tokens.refresh_token)
            )
            if expires_at is not None:
                data["expires_at"] = expires_at
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

    def clear_tokens(self) -> None:
        """Drop only the access/refresh tokens, keeping the DCR client info and
        discovered metadata. Used before an interactive (re-)authorization so the
        provider can't satisfy the probe with an existing valid token — which would
        skip the redirect and hang the flow — while a fresh grant reuses the same
        registered client instead of re-registering. Idempotent."""
        with self._locked():
            data = self._read()
            if not data:
                return
            # Pop BOTH before the check — an ``or`` on the two pops would short-circuit and
            # leave a stale ``expires_at`` behind when a token was present.
            tokens_popped = data.pop("tokens", None) is not None
            expiry_popped = data.pop("expires_at", None) is not None
            if tokens_popped or expiry_popped:
                self._write(data)

    def clear(self) -> None:
        """Delete the credential file entirely (e.g. when the server is removed or the
        operator disconnects the upstream). Idempotent.

        Held under the same lock writers use, so a concurrent bridge refresh can't
        ``os.replace`` a fresh credential file back in *after* the clear returns (which
        would resurrect a disconnected server). The ``.lock`` sidecar is intentionally
        left in place — deleting it while another process may hold it breaks the mutex."""
        with self._locked():
            self.path.unlink(missing_ok=True)
