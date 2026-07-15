"""API request/response models (the control-plane contract the SPA depends on)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from typing import Union

from pydantic import BaseModel, StrictBool, StrictStr

# The auth providers a server may select. Constrained here so a malformed value
# (e.g. "bearer " / "Bearer") is rejected at the API boundary with a 422 rather
# than silently stored and then failed-closed at request time.
AuthProvider = Literal["inherit", "none", "bearer", "oauth"]
EffectiveAuthProvider = Literal["none", "bearer", "oauth"]


class Transports(BaseModel):
    mcp_http: bool
    rest_openapi: bool


class Urls(BaseModel):
    mcp: Optional[str] = None
    rest: Optional[str] = None


class ServerSummary(BaseModel):
    id: str
    slug: str
    name: str
    runner: str
    enabled: bool
    state: str
    transports: Transports
    urls: Urls
    auth: EffectiveAuthProvider = "none"  # effective auth (per-server `inherit` resolved)
    last_error: Optional[str] = None
    pid: Optional[int] = None
    port: Optional[int] = None
    tools_count: int = 0


class OAuthStatus(BaseModel):
    """Upstream-OAuth state for a remote server, surfaced so the UI can prompt the
    operator to authenticate (and warn when a re-auth is due)."""

    enabled: bool = False  # is this server configured to authenticate upstream via OAuth?
    authenticated: bool = False  # are tokens currently stored?
    needs_auth: bool = False  # OAuth is on but no tokens yet — the operator must connect
    expires_at: Optional[float] = None  # access-token expiry (unix seconds), if known
    has_refresh_token: bool = False  # a refresh token exists (silent renewal until it lapses)


class ServerDetail(ServerSummary):
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: Optional[str] = None
    auth_provider: str = "inherit"
    oauth: bool = False
    oauth_scopes: str = ""
    oauth_client_id: Optional[str] = None
    # The client secret is write-only: accepted on create/patch but never echoed back
    # (this response is polled by the UI), so only its presence is exposed.
    oauth_has_client_secret: bool = False
    oauth_status: OAuthStatus = OAuthStatus()
    config_hash: str = ""
    source: str = "manual"
    tools: list[dict] = []


class ServerCreate(BaseModel):
    name: str
    runner: str = "npx"
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: Optional[str] = None
    mcp_http: bool = True
    rest_openapi: bool = False
    auth_provider: AuthProvider = "inherit"
    # Upstream OAuth (remote runner only; forced off elsewhere server-side).
    oauth: bool = False
    oauth_scopes: str = ""
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    enabled: bool = False
    # Provenance. Only a "catalog:<id>" value is honored (a registry install);
    # anything else falls back to "manual" server-side. See servers.create_server.
    source: Optional[str] = None


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    # Changing the slug re-points the server's public /s/<slug>/ URLs; clients that
    # reference the old slug must be re-pointed. Normalized + uniqueness-checked
    # server-side (a reserved word or a slug in use by another server is a 400).
    slug: Optional[str] = None
    runner: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    mcp_http: Optional[bool] = None
    rest_openapi: Optional[bool] = None
    auth_provider: Optional[AuthProvider] = None
    # StrictBool (unlike mcp_http/rest_openapi above): oauth gates a security-sensitive
    # upstream-auth mode, so a truthy-coerced "yes"/1 must never silently flip it on — the
    # SPA sends a real JSON bool.
    oauth: Optional[StrictBool] = None
    oauth_scopes: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None


class ServerClone(BaseModel):
    # Optional label for the copy; defaults server-side to "<source> copy".
    name: Optional[str] = None


class ImportSkipped(BaseModel):
    name: str
    reason: str


class ImportWarning(BaseModel):
    # Non-fatal notes for a created (disabled) server the operator should see before enabling —
    # e.g. a docker `run` option the hardened runner dropped (mount, --network none, --env-file).
    name: str
    warnings: list[str]


class ImportResult(BaseModel):
    created: list[ServerSummary]
    skipped: list[ImportSkipped]
    warnings: list[ImportWarning] = []


ControlPlaneAuthMode = Literal["auto", "always"]


class TokenCreate(BaseModel):
    name: str
    # "all" (default) authorizes every bearer-protected server; a server id restricts
    # the token to that one server; "control" mints a control-plane admin token.
    # Validated in the endpoint (a dangling server id is a 400).
    scope: str = "all"


class TokenInfo(BaseModel):
    id: str
    name: str
    prefix: str
    scope: str
    created_at: datetime


class TokenCreated(TokenInfo):
    token: str  # plaintext — returned exactly once, at creation


class SettingsInfo(BaseModel):
    bind_mode: str
    allowed_hosts: list[str]
    default_auth_provider: str
    control_plane_auth: ControlPlaneAuthMode = "auto"
    allow_private_lan: bool = False
    docker_runner: bool = False
    oauth_config_url: str = ""
    oauth_audience: str = ""
    oauth_allowed_subjects: list[str] = []
    oauth_accept_bearer: bool = False
    oauth_scopes: list[str] = []


class SettingsUpdate(BaseModel):
    bind_mode: Optional[str] = None
    allowed_hosts: Optional[list[str]] = None
    default_auth_provider: Optional[str] = None
    control_plane_auth: Optional[ControlPlaneAuthMode] = None
    oauth_config_url: Optional[str] = None
    oauth_audience: Optional[str] = None
    oauth_allowed_subjects: Optional[list[str]] = None
    oauth_accept_bearer: Optional[StrictBool] = None
    oauth_scopes: Optional[list[str]] = None
    # StrictBool, not bool: Optional[bool] would coerce "yes"/"true"/1 to True at the
    # API boundary, so the registry's isinstance(bool) invariant would never fire for an
    # API caller. Strict keeps the bool-only contract end to end (the SPA sends a JSON bool).
    allow_private_lan: Optional[StrictBool] = None
    # StrictBool for the same reason — the docker runner is root-equivalent, so a coerced
    # truthy value must never flip it on.
    docker_runner: Optional[StrictBool] = None


# ---- Groups (the /g/<name> registry) ----------------------------------------

# A group's members: the wildcard "*" (every registered server, present and future)
# or an explicit, ordered list of server ids. StrictStr so a non-string can't slip in.
GroupMembers = Union[Literal["*"], list[StrictStr]]


class GroupInfo(BaseModel):
    name: str
    members: GroupMembers
    # read-only, derived: the copyable /g/<name>/mcp URL
    url: str


class GroupUpsert(BaseModel):
    members: GroupMembers


class AuthStatus(BaseModel):
    enforced: bool  # is a control token required right now?
    authenticated: bool  # did this request carry a valid control token?


# ---- Catalog (MCP registry browse + install) --------------------------------


class CatalogSource(BaseModel):
    id: str
    label: str
    # "auto": a runnable command can be derived (npm/pypi); "manual": discovery only,
    # the operator supplies the command in the review form.
    install_support: Literal["auto", "manual"]


class CatalogServer(BaseModel):
    """One browse-view row, normalized across upstream directories."""

    source: str
    id: str  # opaque per-source key used to fetch detail
    name: str
    title: str
    description: str = ""
    version: Optional[str] = None
    status: str = "active"
    registry_types: list[str] = []
    installable: bool = False  # at least one stdio package maps to a supported runner
    repository_url: Optional[str] = None
    web_url: Optional[str] = None


class CatalogList(BaseModel):
    source: str
    servers: list[CatalogServer]
    next_cursor: Optional[str] = None  # opaque pagination cursor for the next page


class CatalogVersions(BaseModel):
    # A server's selectable versions, latest first. Empty for sources without versions.
    versions: list[str] = []


class CatalogDraft(BaseModel):
    """A reviewable, ServerCreate-shaped install draft for one package."""

    package_index: int
    registry_type: str
    identifier: str = ""
    version: Optional[str] = None
    runner: Optional[str] = None
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    installable: bool = False
    reason: Optional[str] = None  # why this draft isn't auto-installable, if so
    warnings: list[str] = []  # required/secret values the operator must fill in


class CatalogRemote(BaseModel):
    type: str
    url: str
    headers: dict[str, str] = {}  # prefilled upstream auth headers (required ones scaffolded)
    warnings: list[str] = []  # required/secret/placeholder headers or a templated URL


class CatalogServerMeta(BaseModel):
    name: str
    title: str
    description: str = ""
    version: Optional[str] = None
    status: str = "active"
    repository_url: Optional[str] = None
    web_url: Optional[str] = None


class CatalogDetail(BaseModel):
    source: str
    manual_install: bool = False  # source has no launch spec; complete the form by hand
    notes: list[str] = []
    server: CatalogServerMeta
    drafts: list[CatalogDraft] = []
    remotes: list[CatalogRemote] = []
