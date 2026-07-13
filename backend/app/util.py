"""Small pure helpers — deterministic by construction (no I/O, no globals)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from typing import Any
from urllib.parse import urlsplit


def new_id() -> str:
    return uuid.uuid4().hex


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "server"


# Fixed (not per-value) on purpose: the anchor must be deterministic — same logical
# config, same hash — or the reconciler would bounce every bridge on every boot.
_CONFIG_HASH_SALT = b"mcpelevator.config_hash.v1"


def config_hash(payload: dict[str, Any]) -> str:
    """Stable short hash of a server's launch+exposure config.

    The idempotency anchor: the reconciler restarts a server only when this
    changes. Uses canonical JSON (sorted keys) so the same logical config always
    hashes identically.

    The payload carries operator-supplied credentials (``env`` values / upstream
    headers), and the result is persisted and served by the API — so it is derived
    with scrypt, a memory-hard KDF, instead of a fast digest: a leaked anchor can't
    be dictionary-attacked to recover those values. Only computed on config writes
    and the boot backfill, so the ~70ms cost never sits on a request-serving path.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.scrypt(
        blob.encode("utf-8"), salt=_CONFIG_HASH_SALT, n=2**14, r=8, p=1, dklen=8
    ).hex()


def new_token() -> str:
    """A fresh bearer token. The ``mcpe_`` prefix makes it identifiable."""
    return "mcpe_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def host_only(value: str) -> str:
    """Hostname from a Host header (``host[:port]`` / ``[ipv6][:port]``) or an
    Origin (``scheme://host[:port]``). Returns "" for empty or malformed input
    (e.g. an unmatched IPv6 bracket) so callers fail closed instead of raising."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        if "://" in value:
            return (urlsplit(value).hostname or "").lower()
        # A bare IPv6 literal (>1 colon, unbracketed) must be bracketed for urlsplit
        # to read it as a host instead of host:port; an ordinary host:port has one colon.
        if value.count(":") > 1 and not value.startswith("["):
            value = f"[{value}]"
        return (urlsplit(f"//{value}").hostname or "").lower()
    except ValueError:
        return ""
