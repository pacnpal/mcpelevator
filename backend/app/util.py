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


def config_hash(payload: dict[str, Any]) -> str:
    """Stable short hash of a server's launch+exposure config.

    The idempotency anchor: the reconciler restarts a server only when this
    changes. Uses canonical JSON (sorted keys) so the same logical config always
    hashes identically.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
        target = value if "://" in value else f"//{value}"
        return (urlsplit(target).hostname or "").lower()
    except ValueError:
        return ""
