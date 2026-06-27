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
	| 'stopping';

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
}

// Superset of ServerSummary returned by GET /api/servers/{id}.
export interface ServerDetail extends ServerSummary {
	command: string;
	args: string[];
	env: Record<string, string>;
	cwd: string | null;
	auth_provider: ServerAuthProvider;
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
	env: Record<string, string>;
	cwd?: string | null;
	mcp_http?: boolean;
	rest_openapi?: boolean;
	auth_provider?: ServerAuthProvider;
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

/** Result of POST /api/servers/import. */
export interface ImportResult {
	created: ServerSummary[];
	skipped: ImportSkipped[];
}

/** Shape of one entry in a standard `mcpServers` map. */
export interface McpServerEntry {
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	// Remote-style entries the backend skips on import.
	url?: string;
	type?: string;
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
export type AuthProvider = 'none' | 'bearer';

/** Per-server auth selector: `inherit` resolves to the global default. */
export type ServerAuthProvider = 'inherit' | 'none' | 'bearer';

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
