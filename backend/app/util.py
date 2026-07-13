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


# Bump when the derivation (algorithm/params) or the *value canonicalization* of the
# payload changes without changing its key set — key-set changes roll the tag on
# their own. A stale stored tag is what tells the boot backfill to rehash a row.
_CONFIG_HASH_SCHEME = "scrypt.v1"


def config_hash_tag(payload: dict[str, Any], *, salt: bytes) -> str:
    """Cheap fingerprint of the hash *scheme*: the payload's key set, the derivation
    id, and the install salt — everything that changes a row's hash other than the
    config values themselves. Prefixed to ``config_hash`` output so the boot backfill
    can recognize rows already written by the current scheme and skip them without
    paying a scrypt derivation per server per boot. Keys only — no secret material."""
    material = "|".join(sorted(payload)) + "|" + _CONFIG_HASH_SCHEME
    return hashlib.sha256(material.encode("utf-8") + salt).hexdigest()[:4]


def config_hash(payload: dict[str, Any], *, salt: bytes) -> str:
    """Stable short hash of a server's launch+exposure config.

    The idempotency anchor: the reconciler restarts a server only when this
    changes. Uses canonical JSON (sorted keys) so the same logical config always
    hashes identically.

    The payload carries operator-supplied credentials (``env`` values / upstream
    headers), and the result is persisted and served by the API — so it is derived
    with scrypt, a memory-hard KDF, keyed with a random per-install ``salt`` kept
    off the DB (see ``registry.service``): recovering config secrets from a leaked
    anchor needs the salt file too, and even with it each guess costs a full
    derivation. The salt must be stable across boots — same logical config, same
    hash, or the reconciler would bounce every bridge on every boot.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.scrypt(blob.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=8)
    return f"{config_hash_tag(payload, salt=salt)}.{digest.hex()}"


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
