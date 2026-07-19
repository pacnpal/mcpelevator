// Shared API contract types for the mcpelevator control panel.
// These mirror the FastAPI backend's response shapes exactly (verified against
// the live backend's OpenAPI schema at /openapi.json).

export type Runner = 'npx' | 'uvx' | 'command' | 'docker' | 'remote';

export type ServerState =
	| 'stopped'
	| 'starting'
	| 'running'
	| 'unhealthy'
	| 'failed'
	| 'stopping'
	/** Enabled but quiesced for inactivity; the proxy wakes it on the next request. */
	| 'idle';

export type StartupPhase = 'queued' | 'setup' | 'bridge' | 'readiness' | 'retry_wait';

export interface StartupStatus {
	phase: StartupPhase;
	attempt: number;
	max_attempts: number;
	activation_started_at: string;
	deadline_at: string | null;
	next_retry_at: string | null;
	message: string | null;
}

export interface ServerTransports {
	mcp_http: boolean;
	rest_openapi: boolean;
}

export interface ServerUrls {
	mcp: string | null;
	rest: string | null;
}

export interface ServerSummary {
	id: string;
	slug: string;
	name: string;
	runner: Runner;
	enabled: boolean;
	state: ServerState;
	startup_status: StartupStatus | null;
	transports: ServerTransports;
	urls: ServerUrls;
	/** Effective auth (per-server `inherit` resolved to the global default). */
	auth: AuthProvider;
	last_error: string | null;
	pid: number | null;
	port: number | null;
	tools_count: number;
}

/** A tool discovered on a running server. */
export interface ServerTool {
	name: string;
	description: string;
	/** The tool's JSON input schema (from the readiness probe). The playground
	 * builds its argument form from this. Absent on tool lists cached before the
	 * field existed. */
	input_schema?: Record<string, unknown>;
	/** Whether the tool declares an MCP `outputSchema`. Schemas are authored by
	 * the upstream server and proxied through unchanged; MCP clients recommend
	 * tools declare one so models can better understand results.
	 * Absent on tool lists cached before this field existed. */
	has_output_schema?: boolean;
}

/** Result of POST /api/servers/{id}/tools/{name}/call (the tool playground).
 * MCP semantics: `is_error` carries the tool's own failure — the call itself
 * transported fine (HTTP 200). */
export interface ToolCallResult {
	is_error: boolean;
	/** Raw MCP content blocks (TextContent, ImageContent, …). */
	content: Record<string, unknown>[];
	structured_content: Record<string, unknown> | null;
	duration_ms: number;
}

/** Upstream-OAuth state for a remote server (GET /api/servers/{id}). */
export interface OAuthStatus {
	/** Is this server configured to authenticate upstream via OAuth? */
	enabled: boolean;
	/** Are tokens currently stored (the operator has signed in)? */
	authenticated: boolean;
	/** OAuth is on but no tokens yet — the operator must connect the provider. */
	needs_auth: boolean;
	/** Access-token expiry (unix seconds), if known. */
	expires_at: number | null;
	/** A refresh token exists — renewal is silent until it lapses. */
	has_refresh_token: boolean;
}

// Superset of ServerSummary returned by GET /api/servers/{id}.
export interface ServerDetail extends ServerSummary {
	command: string;
	args: string[];
	/** Docker runner only: extra `docker run` options placed before the image
	 * (e.g. --name, --shm-size=1g). Always [] for other runners. */
	run_args: string[];
	setup_script: string;
	env: Record<string, string>;
	cwd: string | null;
	auth_provider: ServerAuthProvider;
	/** Remote runner: authenticate to the upstream via OAuth instead of static headers. */
	oauth: boolean;
	oauth_scopes: string;
	oauth_client_id: string | null;
	/** Whether a static client secret is stored. The secret itself is write-only —
	 * accepted on create/patch but never returned. */
	oauth_has_client_secret: boolean;
	oauth_status: OAuthStatus;
	/** Idle quiescence override in seconds: null = inherit the global setting,
	 * 0 = never idle this server out. */
	idle_timeout_s: number | null;
	config_hash: string;
	source: string;
	tools: ServerTool[];
}

// Request body for POST /api/servers. PATCH accepts any subset of these
// fields except `enabled` (see ServerUpdate).
export interface ServerCreate {
	name: string;
	runner: Runner;
	command: string;
	args: string[];
	/** Docker runner only: extra `docker run` options placed before the image.
	 * Forbidden options (-d, -e/--env/--env-file, the reserved reaping label, '--')
	 * are rejected with a 400; forced [] for non-docker runners server-side. */
	run_args?: string[];
	setup_script?: string;
	env: Record<string, string>;
	cwd?: string | null;
	mcp_http?: boolean;
	rest_openapi?: boolean;
	auth_provider?: ServerAuthProvider;
	/** Remote runner: authenticate upstream via OAuth (forced off for other runners). */
	oauth?: boolean;
	oauth_scopes?: string;
	oauth_client_id?: string | null;
	oauth_client_secret?: string | null;
	/** Idle quiescence override in seconds (null = inherit, 0 = never idle). */
	idle_timeout_s?: number | null;
	enabled?: boolean;
	/** Provenance. Only a `catalog:<id>` value is honored server-side (a registry install). */
	source?: string | null;
}

/**
 * PATCH /api/servers/{id} accepts any subset of the create fields except `enabled`,
 * plus an optional `slug` rename. Changing the slug re-points the server's public
 * URLs — clients referencing the old slug must be updated.
 */
export type ServerUpdate = Partial<Omit<ServerCreate, 'enabled'>> & {
	slug?: string;
};

/** A single entry the importer declined to create, with a human reason. */
export interface ImportSkipped {
	name: string;
	reason: string;
}

/** Non-fatal notes for a created (disabled) server — e.g. docker run options the hardened
 *  runner dropped (mount, --network none, --env-file) the operator should see before enabling. */
export interface ImportWarning {
	name: string;
	warnings: string[];
}

/** Result of POST /api/servers/import. */
export interface ImportResult {
	created: ServerSummary[];
	skipped: ImportSkipped[];
	warnings?: ImportWarning[];
}

/** Shape of one entry in a standard `mcpServers` map. */
export interface McpServerEntry {
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	// Remote-style entries: the backend imports these as proxied "remote" servers.
	url?: string;
	httpUrl?: string; // Gemini CLI's Streamable-HTTP shape (alias for a streamable-http url)
	type?: string;
	transport?: string; // alias the backend accepts in place of `type`
	headers?: Record<string, string>;
}

export interface HealthResponse {
	status: string;
	version: string;
}

// ---- Catalog (browse upstream MCP directories + install) --------------------

/** An upstream directory we can browse. `auto` derives a runnable command;
 * `manual` is discovery-only (the operator fills in the command). */
export interface CatalogSource {
	id: string;
	label: string;
	install_support: 'auto' | 'manual';
}

/** One browse-view row, normalized across sources (GET /api/catalog/servers). */
export interface CatalogServer {
	source: string;
	/** Opaque per-source key used to fetch detail. */
	id: string;
	name: string;
	title: string;
	description: string;
	version: string | null;
	status: string;
	registry_types: string[];
	/** At least one stdio package maps to a supported runner (npm/pypi). */
	installable: boolean;
	repository_url: string | null;
	web_url: string | null;
}

export interface CatalogList {
	source: string;
	servers: CatalogServer[];
	next_cursor: string | null;
}

/** A server's selectable versions, latest first (empty for sources without versions). */
export interface CatalogVersions {
	versions: string[];
}

/** A reviewable, ServerCreate-shaped install draft for one package. */
export interface CatalogDraft {
	package_index: number;
	registry_type: string;
	identifier: string;
	version: string | null;
	runner: Runner | null;
	command: string;
	args: string[];
	env: Record<string, string>;
	installable: boolean;
	/** Why this draft isn't auto-installable, if so. */
	reason: string | null;
	/** Required/secret values the operator must fill in before starting. */
	warnings: string[];
}

export interface CatalogRemote {
	type: string;
	url: string;
	/** Prefilled upstream auth headers (required ones scaffolded, possibly empty). */
	headers: Record<string, string>;
	/** Required/secret/placeholder headers or a templated URL the operator must fix. */
	warnings: string[];
}

export interface CatalogServerMeta {
	name: string;
	title: string;
	description: string;
	version: string | null;
	status: string;
	repository_url: string | null;
	web_url: string | null;
}

export interface CatalogDetail {
	source: string;
	/** Source has no launch spec; the operator completes the form by hand. */
	manual_install: boolean;
	notes: string[];
	server: CatalogServerMeta;
	drafts: CatalogDraft[];
	remotes: CatalogRemote[];
}

// ---- Auth & settings (M5) ---------------------------------------------------

/** How the server socket is bound. `expose` enforces the Host/Origin allowlist. */
export type BindMode = 'local' | 'expose';

/** Auth provider for the *global default* and per-server `inherit` resolution. */
export type AuthProvider = 'none' | 'bearer' | 'oauth';

/** Per-server auth selector: `inherit` resolves to the global default. */
export type ServerAuthProvider = 'inherit' | 'none' | 'bearer' | 'oauth';

/** Control-plane auth enforcement: `auto` requires a token only when exposed,
 * `always` requires one even on loopback. */
export type ControlPlaneAuth = 'auto' | 'always';

/** Shape of GET/PATCH /api/settings. */
export interface SettingsInfo {
	bind_mode: BindMode;
	allowed_hosts: string[];
	default_auth_provider: AuthProvider;
	control_plane_auth: ControlPlaneAuth;
	/** Allow private-IP-literal Hosts from a LAN peer (self-hosted box access).
	 * Rebinding-safe; turns control-plane `auto` enforcement on while enabled. */
	allow_private_lan: boolean;
	/** Enable the docker runner (launch MCP servers packaged as Docker/OCI images).
	 * OFF by default and root-equivalent — it runs arbitrary images on the mounted
	 * Docker daemon. Gates docker-server enable/start and OCI catalog installs. */
	docker_runner: boolean;
	/** External authorization-server discovery URL for inbound OAuth. */
	oauth_config_url: string;
	/** Required JWT audience for inbound OAuth access tokens. */
	oauth_audience: string;
	/** Optional token identities allowed to use OAuth-protected endpoints. */
	oauth_allowed_subjects: string[];
	/** Also accept local mcpe_ bearer tokens on OAuth-protected endpoints. */
	oauth_accept_bearer: boolean;
	/** Scopes advertised in RFC 9728 protected-resource metadata. */
	oauth_scopes: string[];
	/** Default idle quiescence in seconds for servers set to inherit (0 = off). */
	idle_timeout_s: number;
}

/** A group's members: the wildcard "*" (every registered server, present and
 * future) or an explicit, ordered list of server ids. */
export type GroupMembers = '*' | string[];

/** A named group served at /g/<name>/mcp (GET/PUT /api/groups). */
export interface GroupInfo {
	name: string;
	members: GroupMembers;
	/** Read-only, derived by the backend: the copyable /g/<name>/mcp URL. */
	url: string;
}

/** Shape of GET /api/auth/status — the SPA polls this to decide whether to show login. */
export interface AuthStatus {
	enforced: boolean;
	authenticated: boolean;
}

/** A bearer access token, listed by prefix only (the plaintext is never re-shown). */
export interface TokenInfo {
	id: string;
	name: string;
	prefix: string;
	/** `'all'` = every bearer-protected server; a server id = that one server; `'control'` = a control-plane admin token. */
	scope: string;
	created_at: string;
}

/**
 * Response of POST /api/tokens. Identical to `TokenInfo` but additionally
 * carries the full plaintext `token` — returned exactly once, on creation.
 */
export type TokenCreated = TokenInfo & { token: string };
