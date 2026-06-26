// Shared API contract types for the mcpelevator control panel.
// These mirror the FastAPI backend's response shapes. The detail type
// intentionally tolerates missing fields (everything optional beyond the
// summary) so the UI degrades gracefully against a partial backend.

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
	last_error: string | null;
}

// Superset of ServerSummary. Every additional field is optional because the
// backend may not populate all of them yet; consumers must null-check.
export interface ServerDetail extends ServerSummary {
	description?: string | null;
	command?: string | null;
	args?: string[] | null;
	env?: Record<string, string> | null;
	cwd?: string | null;
	image?: string | null;
	version?: string | null;
	created_at?: string | null;
	updated_at?: string | null;
	started_at?: string | null;
	pid?: number | null;
	restart_count?: number | null;
	tools?: string[] | null;
	tags?: string[] | null;
}

export interface HealthResponse {
	status: string;
	version: string;
}
