"""API request/response models (the control-plane contract the SPA depends on)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from typing import Union

from pydantic import BaseModel, StrictBool, StrictInt, StrictStr

# The auth providers a server may select. Constrained here so a malformed value
# (e.g. "bearer " / "Bearer") is rejected at the API boundary with a 422 rather
# than silently stored and then failed-closed at request time.
AuthProvider = Literal["inherit", "none", "bearer", "oauth"]
EffectiveAuthProvider = Literal["none", "bearer", "oauth"]
StartupPhase = Literal["queued", "setup", "bridge", "readiness", "retry_wait"]


class Transports(BaseModel):
    mcp_http: bool
    rest_openapi: bool


class Urls(BaseModel):
    mcp: Optional[str] = None
    rest: Optional[str] = None


class StartupStatus(BaseModel):
    phase: StartupPhase
    attempt: int
    max_attempts: int
    activation_started_at: datetime
    deadline_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    message: Optional[str] = None


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
    startup_status: Optional[StartupStatus] = None
    # Ownership (multi-user): None = admin-owned. owner_name is denormalized so
    # the admin dashboard can label rows without a second fetch.
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None


class OAuthStatus(BaseModel):
    """Upstream-OAuth state for a remote server, surfaced so the UI can prompt the
    operator to authenticate (and warn when a re-auth is due)."""

    enabled: bool = False  # is this server configured to authenticate upstream via OAuth?
    authenticated: bool = False  # are tokens currently stored?
    needs_auth: bool = False  # OAuth is on but no tokens yet — the operator must connect
    expires_at: Optional[float] = None  # access-token expiry (unix seconds), if known
    has_refresh_token: bool = False  # a refresh token exists (silent renewal until it lapses)


class ToolCallRequest(BaseModel):
    """Playground invocation of one tool on a running server's bridge."""

    arguments: dict = {}
    # Per-call wall clock in seconds; bounded so a hung upstream can't pin the
    # control plane worker forever.
    timeout_s: float = 60.0


class ToolCallResult(BaseModel):
    """MCP semantics mirrored over the control plane: ``is_error`` carries a tool's
    own failure (the transport succeeded), matching CallToolResult.isError."""

    is_error: bool = False
    # Raw MCP content blocks (model_dump of TextContent / ImageContent / …) so the
    # UI can render text and fall back to JSON for anything else.
    content: list[dict] = []
    structured_content: Optional[dict] = None
    duration_ms: int = 0


class ServerDetail(ServerSummary):
    command: str
    args: list[str] = []
    # docker runner only: extra `docker run` options placed before the image
    # (e.g. --name, --shm-size=1g). Always [] for other runners.
    run_args: list[str] = []
    env: dict[str, str] = {}
    cwd: Optional[str] = None
    setup_script: str = ""
    auth_provider: str = "inherit"
    oauth: bool = False
    oauth_scopes: str = ""
    oauth_client_id: Optional[str] = None
    # The client secret is write-only: accepted on create/patch but never echoed back
    # (this response is polled by the UI), so only its presence is exposed.
    oauth_has_client_secret: bool = False
    oauth_status: OAuthStatus = OAuthStatus()
    # Idle quiescence: None = inherit the global `idle_timeout_s` setting; 0 = never.
    idle_timeout_s: Optional[int] = None
    # Upstream tool names hidden from every exposed surface. Empty = expose all (default).
    disabled_tools: list[str] = []
    config_hash: str = ""
    source: str = "manual"
    tools: list[dict] = []


class ServerCreate(BaseModel):
    name: str
    runner: str = "npx"
    command: str
    args: list[str] = []
    # docker runner only: extra `docker run` options placed before the image. A
    # forbidden option (-d, -e/--env/--env-file, the reserved reaping label, '--')
    # is a 400; the value is forced [] for non-docker runners server-side.
    run_args: list[str] = []
    env: dict[str, str] = {}
    cwd: Optional[str] = None
    setup_script: str = ""
    mcp_http: bool = True
    rest_openapi: bool = False
    # Upstream tool names to hide from every exposed surface (empty = expose all).
    # Normalized (trimmed, deduped, sorted) server-side.
    disabled_tools: list[StrictStr] = []
    auth_provider: AuthProvider = "inherit"
    # Upstream OAuth (remote runner only; forced off elsewhere server-side).
    oauth: bool = False
    oauth_scopes: str = ""
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    # Idle quiescence: None = inherit the global setting; 0 = never idle out.
    # StrictInt: lax mode coerces a JSON `true` to 1, which would silently
    # configure a one-second shutdown instead of failing validation.
    idle_timeout_s: Optional[StrictInt] = None
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
    run_args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    setup_script: Optional[str] = None
    mcp_http: Optional[bool] = None
    rest_openapi: Optional[bool] = None
    # Replace the whole hide list; [] re-exposes every tool. Omitted (null) = unchanged.
    disabled_tools: Optional[list[StrictStr]] = None
    auth_provider: Optional[AuthProvider] = None
    # StrictBool (unlike mcp_http/rest_openapi above): oauth gates a security-sensitive
    # upstream-auth mode, so a truthy-coerced "yes"/1 must never silently flip it on — the
    # SPA sends a real JSON bool.
    oauth: Optional[StrictBool] = None
    oauth_scopes: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    # Nullable like the OAuth client fields: an explicit null means "inherit the
    # global default" and is preserved by the PATCH handler (model_fields_set).
    # StrictInt so a JSON `true` can't lax-coerce to a 1-second shutdown.
    idle_timeout_s: Optional[StrictInt] = None
    # Admin-only reassignment; explicit null = make it admin-owned. Ignored (403)
    # for members. Preserved-on-null like the OAuth client fields.
    owner_id: Optional[str] = None


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
    # Owning user (None for tokens minted before multi-user, by boot, or by a
    # synthetic admin). user_name is denormalized for the admin token table.
    user_id: Optional[str] = None
    user_name: Optional[str] = None
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
    # Default idle quiescence for servers whose idle_timeout_s is unset (0 = off).
    idle_timeout_s: int = 0


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
    # Global default for idle quiescence in seconds (0 disables it). StrictInt so
    # a JSON `true` can't lax-coerce to a 1-second shutdown.
    idle_timeout_s: Optional[StrictInt] = None


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


Role = Literal["admin", "member"]


class AuthUser(BaseModel):
    """The authenticated principal, as the SPA sees it. ``id`` is None for the
    synthetic admins (enforcement off / MCPE_ADMIN_TOKEN / a legacy user-less
    control token) — always role=admin, so the UI shows the full surface."""

    id: Optional[str] = None
    name: str
    role: Role
    local_runners: bool = True


class AuthStatus(BaseModel):
    enforced: bool  # is a control token required right now?
    authenticated: bool  # did this request carry a valid control token?
    user: Optional[AuthUser] = None  # the resolved principal when authenticated


# ---- Users (multi-user control plane) ---------------------------------------


class UserInfo(BaseModel):
    id: str
    name: str
    role: Role
    local_runners: bool
    servers_count: int = 0  # servers owned (drives the delete guard in the UI)
    tokens_count: int = 0  # tokens bound to this user (control + data-plane)
    created_at: datetime


class UserCreate(BaseModel):
    name: str
    role: Role = "member"
    # StrictBool: gates code execution on this box — a truthy-coerced value must
    # never silently widen an account (same rationale as docker_runner).
    local_runners: StrictBool = False


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[Role] = None
    local_runners: Optional[StrictBool] = None


class UserCredential(BaseModel):
    """A freshly minted login (control) token for a user — plaintext exactly once."""

    token_id: str
    token: str
    prefix: str


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
