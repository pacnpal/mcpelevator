// Shared API contract types for the mcpelevator control panel.
// These mirror the FastAPI backend's response shapes exactly (verified against
// the live backend's OpenAPI schema at /openapi.json).

export type Runner = 'npx' | 'uvx' | 'command' | 'docker';

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
}

/** PATCH /api/servers/{id} accepts any subset of the create fields except `enabled`. */
export type ServerUpdate = Partial<Omit<ServerCreate, 'enabled'>>;

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
}

/** Shape of GET /api/auth/status — the SPA polls this to decide whether to show login. */
export interface AuthStatus {
	enforced: boolean;
	authenticated: boolean;
}

/** A token's scope: `proxy` (per-server data plane) or `control` (the /api control plane). */
export type TokenScope = 'proxy' | 'control';

/** A bearer access token, listed by prefix only (the plaintext is never re-shown). */
export interface TokenInfo {
	id: string;
	name: string;
	prefix: string;
	scope: TokenScope;
	created_at: string;
}

/**
 * Response of POST /api/tokens. Identical to `TokenInfo` but additionally
 * carries the full plaintext `token` — returned exactly once, on creation.
 */
export type TokenCreated = TokenInfo & { token: string };
