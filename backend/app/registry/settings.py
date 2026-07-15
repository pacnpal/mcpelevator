"""Runtime-mutable settings, stored in the SQLite ``setting`` kv table.

Distinct from ``app.config`` (process/env bootstrap settings): these are the
security knobs the UI edits at runtime — bind mode, the Host/Origin allowlist,
and the default auth provider for servers set to ``inherit``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import Session

from app.db import repo
from app.util import host_only

DEFAULTS: dict[str, Any] = {
    "bind_mode": "local",  # 'local' | 'expose'
    "allowed_hosts": [],  # Host/Origin allowlist when exposed (DNS-rebinding defense)
    "default_auth_provider": "none",  # 'none' | 'bearer' — used when a server is 'inherit'
    "control_plane_auth": "auto",  # 'auto' (require iff expose) | 'always' — gates /api bearer auth
    # Allow private-IP-literal Hosts (e.g. http://192.168.1.50:8080) from a peer on a
    # private network, so a self-hosted box (Unraid, NAS) is reachable from other LAN
    # devices without per-host allowlisting. Rebinding-safe (a rebound attack sends a
    # domain, not a private-IP literal). Counts as "reachable off-host" so 'auto'
    # control-plane auth turns on when it's enabled — see app.auth.control_plane.
    "allow_private_lan": False,
    # Enable the docker runner (launch MCP servers packaged as Docker/OCI images). OFF by
    # default and ROOT-EQUIVALENT: it runs arbitrary images on the mounted Docker daemon
    # (sibling containers on the host, or an isolated dind sidecar via DOCKER_HOST). The
    # service/supervisor refuse to enable or start a docker server while this is off.
    "docker_runner": False,
    # The group registry — the single source of truth for what /g/<name>/mcp serves.
    # A mapping from group name to either "*" (every registered server, present and
    # future) or an ordered list of server ids. EMPTY by default: a group is one URL
    # that reaches many servers' tools, so exposed setups must declare each one
    # deliberately. There is no special-case name — add {"all": "*"} for a bundle of
    # everything. See app.groups.registry for the resolution + validation rules.
    "groups": {},
    # --- oauth provider (RFC 9728 resource server; see app.auth.oauth) ---
    # OIDC discovery / RFC 8414 metadata URL of the EXTERNAL authorization server
    # (a bare issuer URL also works). Empty = oauth provider unconfigured; servers
    # set to oauth then fail closed (403).
    "oauth_config_url": "",
    # ``aud`` claim required in access tokens. Empty leaves OAuth unconfigured and
    # fails closed, so a token minted for another resource is never accepted.
    "oauth_audience": "",
    # Optional identity allowlist. Friendly username/login/email claims match
    # case-insensitively; OIDC subject identifiers match exactly.
    "oauth_allowed_subjects": [],
    # Scopes advertised as ``scopes_supported`` in the Protected Resource Metadata.
    # MCP clients build their authorize request from this (per the MCP auth spec), so
    # it steers which claims the AS puts in tokens — e.g. ["openid","profile","email"]
    # makes preferred_username/email available to oauth_allowed_subjects. Empty = omit.
    "oauth_scopes": [],
    # Also accept LOCAL bearer tokens ("mcpe_..."-prefixed) on oauth-protected
    # servers, with normal bearer scope semantics — one endpoint for OAuth humans
    # and token-carrying automation. Off by default: pure-OAuth unless opted in.
    "oauth_accept_bearer": False,
}


def read_all(session: Session) -> dict[str, Any]:
    return {key: repo.setting_get(session, key, default) for key, default in DEFAULTS.items()}


_MODES = {"local", "expose"}
_PROVIDERS = {"none", "bearer", "oauth"}
_CONTROL_PLANE_AUTH_MODES = {"auto", "always"}


# Group names are the routing key in /g/<name>/mcp, so they must be URL-safe: lowercase
# alphanumerics and single hyphens (the slugify() vocabulary), non-empty. Validated here
# so a name that couldn't be routed can never be stored.
_GROUP_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def is_valid_oauth_endpoint_url(value: Any) -> bool:
    """OAuth metadata and JWKS endpoints require HTTPS, except for loopback dev."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # accessing .port rejects malformed values such as ":bad"
    except ValueError:
        return False
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _normalize_group_members(name: str, value: Any) -> Any:
    """Validate one group's member value: the literal ``"*"`` (every registered
    server) or a list of server-id strings (deduped, order kept). Structural only —
    referential validation (ids must be registered servers) lives in
    ``app.groups.registry`` so create/delete can prune without a circular import."""
    if value == "*":
        return "*"
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"invalid members for group {name!r}: {value!r}")
    out: list[str] = []
    for v in value:
        if v not in out:
            out.append(v)
    return out


def _normalize_groups(value: Any) -> dict[str, Any]:
    """Validate the group registry: a mapping from a URL-safe group name to a member
    value (see ``_normalize_group_members``). Rejects a malformed name or member shape
    so a value that couldn't be routed or resolved can never be stored."""
    if not isinstance(value, dict):
        raise ValueError(f"groups must be an object, got {value!r}")
    out: dict[str, Any] = {}
    for name, members in value.items():
        if not isinstance(name, str) or not _GROUP_NAME_RE.match(name):
            raise ValueError(f"invalid group name {name!r} (use lowercase letters, digits, hyphens)")
        out[name] = _normalize_group_members(name, members)
    return out


def _normalize_hosts(hosts: Any) -> list[str]:
    """Validate + normalize allowlist entries to bare hostnames, rejecting empties
    and malformed values (e.g. an unmatched IPv6 bracket) so a bad entry can never
    be stored and later crash the allowlist check. A pasted URL/host:port is
    reduced to its hostname."""
    if not isinstance(hosts, list):
        raise ValueError("allowed_hosts must be a list")
    out: list[str] = []
    for h in hosts:
        host = host_only(h) if isinstance(h, str) else ""
        if not host:
            raise ValueError(f"invalid allowed host: {h!r}")
        if host not in out:  # dedupe post-normalization (e.g. host vs host:port); keep order
            out.append(host)
    return out


def write(
    session: Session, changes: dict[str, Any], *, guard: Callable[[Session], None] | None = None
) -> dict[str, Any]:
    """Persist setting changes. Validates + normalizes the WHOLE patch first, then
    commits it in one step (the SSOT write path), so a bad value can never reach
    storage and a multi-field patch never *partially* applies — a partial write
    could e.g. flip bind_mode and lock out the control plane while still erroring."""
    pending: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key == "bind_mode" and value not in _MODES:
            raise ValueError(f"invalid bind_mode: {value!r}")
        if key == "default_auth_provider" and value not in _PROVIDERS:
            raise ValueError(f"invalid default_auth_provider: {value!r}")
        if key == "control_plane_auth" and value not in _CONTROL_PLANE_AUTH_MODES:
            raise ValueError(f"invalid control_plane_auth: {value!r}")
        if key == "allow_private_lan" and not isinstance(value, bool):
            raise ValueError(f"invalid allow_private_lan: {value!r}")
        if key == "docker_runner" and not isinstance(value, bool):
            raise ValueError(f"invalid docker_runner: {value!r}")
        if key == "groups":
            value = _normalize_groups(value)
        if key == "oauth_config_url":
            if not isinstance(value, str):
                raise ValueError(f"invalid oauth_config_url: {value!r}")
            value = value.strip()
            if value and not is_valid_oauth_endpoint_url(value):
                raise ValueError(f"invalid oauth_config_url: {value!r}")
        if key == "oauth_audience":
            if not isinstance(value, str):
                raise ValueError(f"invalid oauth_audience: {value!r}")
            value = value.strip()
        if key == "oauth_allowed_subjects":
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError(f"invalid oauth_allowed_subjects: {value!r}")
            deduped: list[str] = []
            for v in (x.strip() for x in value):
                if v not in deduped:
                    deduped.append(v)
            value = deduped
        if key == "oauth_accept_bearer" and not isinstance(value, bool):
            raise ValueError(f"invalid oauth_accept_bearer: {value!r}")
        if key == "oauth_scopes":
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError(f"invalid oauth_scopes: {value!r}")
            scopes: list[str] = []
            for v in (x.strip() for x in value):
                if v not in scopes:
                    scopes.append(v)
            value = scopes
        if key == "allowed_hosts":
            value = _normalize_hosts(value)
        pending[key] = value
    repo.setting_set_many(session, pending, guard=guard)
    return read_all(session)


def bind_mode(session: Session) -> str:
    return repo.setting_get(session, "bind_mode", DEFAULTS["bind_mode"])


def allowed_hosts(session: Session) -> list[str]:
    return repo.setting_get(session, "allowed_hosts", DEFAULTS["allowed_hosts"])


def default_auth_provider(session: Session) -> str:
    return repo.setting_get(session, "default_auth_provider", DEFAULTS["default_auth_provider"])


def control_plane_auth(session: Session) -> str:
    return repo.setting_get(session, "control_plane_auth", DEFAULTS["control_plane_auth"])


def oauth_config_url(session: Session) -> str:
    return repo.setting_get(session, "oauth_config_url", DEFAULTS["oauth_config_url"])


def oauth_audience(session: Session) -> str:
    return repo.setting_get(session, "oauth_audience", DEFAULTS["oauth_audience"])


def oauth_allowed_subjects(session: Session) -> list[str]:
    return repo.setting_get(session, "oauth_allowed_subjects", DEFAULTS["oauth_allowed_subjects"])


def oauth_scopes(session: Session) -> list[str]:
    return repo.setting_get(session, "oauth_scopes", DEFAULTS["oauth_scopes"])


def oauth_accept_bearer(session: Session) -> bool:
    return repo.setting_get(session, "oauth_accept_bearer", DEFAULTS["oauth_accept_bearer"])


def allow_private_lan(session: Session) -> bool:
    return repo.setting_get(session, "allow_private_lan", DEFAULTS["allow_private_lan"])


def docker_runner(session: Session) -> bool:
    return repo.setting_get(session, "docker_runner", DEFAULTS["docker_runner"])


def groups(session: Session) -> dict[str, Any]:
    """The group registry: ``{name: "*" | [server_id, ...]}`` (see DEFAULTS)."""
    return repo.setting_get(session, "groups", DEFAULTS["groups"])
