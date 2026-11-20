// Typed fetch helpers for the mcpelevator backend.
//
// All requests target the same-origin `/api` base. In dev, Vite proxies this
// to the FastAPI backend (see vite.config.ts); in production the backend
// serves both the API and the built SPA, so the path is identical.
//
// Every helper throws `ApiError` on a non-2xx response, carrying the status
// and the response body text so callers can surface a useful message.

import { goto } from '$app/navigation';

import { clearToken, getToken } from './auth';
import type {
	AuthStatus,
	CacheClearResult,
	CatalogDetail,
	CatalogList,
	CatalogSource,
	CatalogVersions,
	GroupInfo,
	GroupMembers,
	GroupRestartResult,
	HealthResponse,
	ImportResult,
	InstanceUsage,
	GraphqlPreview,
	OpenapiPreview,
	ServerCreate,
	ServerDetail,
	ServerSummary,
	ServerUpdate,
	ServerUsage,
	SettingsInfo,
	TokenCreated,
	TokenInfo,
	ToolCallResult,
	UserCredential,
	UserInfo
} from './types';

const BASE = '/api';

export class ApiError extends Error {
	readonly status: number;
	readonly body: string;

	constructor(status: number, body: string, url: string) {
		super(`${status} ${url} — ${body || 'request failed'}`);
		this.name = 'ApiError';
		this.status = status;
		this.body = body;
	}
}

/**
 * Extract a human-friendly message from an unknown error. For ApiError this
 * unwraps FastAPI's `{ "detail": ... }` envelope when present, so form errors
 * read cleanly instead of dumping raw JSON.
 */
export function errorMessage(err: unknown): string {
	if (err instanceof ApiError) {
		const text = err.body?.trim();
		if (text) {
			try {
				const parsed = JSON.parse(text);
				const detail = parsed?.detail;
				if (typeof detail === 'string') return detail;
				if (Array.isArray(detail)) {
					// FastAPI validation errors: [{ loc, msg, ... }, ...]
					const msgs = detail
						.map((d) => (typeof d?.msg === 'string' ? d.msg : null))
						.filter(Boolean);
					if (msgs.length) return msgs.join('; ');
				}
				if (typeof parsed?.message === 'string') return parsed.message;
			} catch {
				// Not JSON — fall through and use the raw text.
			}
			return text;
		}
		return `Request failed (${err.status})`;
	}
	if (err instanceof Error) return err.message;
	return 'Unexpected error';
}

// The Sign-in session's CSRF header value, from the latest /auth/status (ADR-0004).
let csrf: string | null = null;
// The first status fetch. Every other request waits for it, so a signed-in browser's
// first request already carries the CSRF header.
let firstStatus: Promise<unknown> | null = null;

/**
 * The credential headers for an `/api` request: the pasted login token as a bearer,
 * and `X-MCPE-CSRF` once status has handed one out. The session cookie itself rides
 * on `fetch`'s default same-origin credentials.
 */
async function authHeaders(waitForStatus = true, withToken = true): Promise<Record<string, string>> {
	if (waitForStatus) {
		if (!firstStatus) getAuthStatus().catch(() => undefined);
		await firstStatus;
	}
	const token = withToken ? getToken() : null;
	return {
		...(token ? { authorization: `Bearer ${token}` } : {}),
		...(csrf ? { 'x-mcpe-csrf': csrf } : {})
	};
}

/**
 * Every `/api` fetch goes through here: credential headers, and the Sign-in session
 * refresh. A session past its provider access token gets 401 everywhere but
 * /auth/status, which refreshes it, so a 401 asks status once and retries once. A 401
 * that survives that drops the token and bounces to /login (unless already there).
 * Returns the ok response; throws `ApiError` otherwise.
 */
async function apiFetch(path: string, init?: RequestInit, withToken = true): Promise<Response> {
	const url = `${BASE}${path}`;
	const send = async () =>
		fetch(url, {
			...init,
			headers: {
				accept: 'application/json',
				...(await authHeaders(path !== '/auth/status', withToken)),
				...init?.headers
			}
		});
	let res = await send();
	if (res.status === 401 && path !== '/auth/status') {
		const refreshed = await getAuthStatus(withToken).then(
			() => true,
			() => false
		);
		if (refreshed) res = await send();
	}

	if (!res.ok) {
		if (res.status === 401) {
			clearToken();
			if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
				void goto('/login');
			}
		}
		let body = '';
		try {
			body = await res.text();
		} catch {
			// ignore — body may be unreadable on some error responses
		}
		throw new ApiError(res.status, body, url);
	}
	return res;
}

async function request<T>(path: string, init?: RequestInit, withToken = true): Promise<T> {
	const res = await apiFetch(path, init, withToken);
	// 204 / empty bodies: return undefined cast to T.
	if (res.status === 204) return undefined as T;
	return (await res.json()) as T;
}

/** JSON-bodied request helper (sets content-type + serializes the body). */
function jsonRequest<T>(
	path: string,
	method: string,
	body: unknown
): Promise<T> {
	return request<T>(path, {
		method,
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body)
	});
}

export function getHealth(): Promise<HealthResponse> {
	return request<HealthResponse>('/health');
}

export function listServers(): Promise<ServerSummary[]> {
	return request<ServerSummary[]>('/servers');
}

/** `includeDocument`: also return an openapi server's stored document or a graphql
 * server's stored schema (edit form only;
 * it can be megabytes, so the polled detail view leaves it out). */
export function getServer(id: string, includeDocument = false): Promise<ServerDetail> {
	const q = includeDocument ? '?include_document=true' : '';
	return request<ServerDetail>(`/servers/${encodeURIComponent(id)}${q}`);
}

/**
 * Usage counters for one server: totals, per tool, and a series to chart.
 *
 * @param id - The server ID.
 * @param days - Trailing window in days (the backend rolls long windows up to
 * daily buckets and never reaches past the retention setting).
 * @returns The server's usage over that window.
 */
export function getServerUsage(id: string, days = 7): Promise<ServerUsage> {
	const q = new URLSearchParams({ days: String(days) });
	return request<ServerUsage>(`/servers/${encodeURIComponent(id)}/usage?${q.toString()}`);
}

/**
 * Instance-wide usage: totals, a series to chart, and per-server / per-tool
 * rollups across every server the caller can see.
 *
 * @param days - Trailing window in days.
 * @returns Usage for the whole instance over that window.
 */
export function getInstanceUsage(days = 7): Promise<InstanceUsage> {
	const q = new URLSearchParams({ days: String(days) });
	return request<InstanceUsage>(`/usage?${q.toString()}`);
}

export function createServer(body: ServerCreate): Promise<ServerSummary> {
	return jsonRequest<ServerSummary>('/servers', 'POST', body);
}

/** What a graphql server would serve: its query and mutation fields, read from the pasted
 * schema, the schema URL, or the endpoint's introspection — all without credentials. */
export function previewGraphql(
	command: string,
	args: string[],
	graphqlSchema: string
): Promise<GraphqlPreview> {
	return jsonRequest<GraphqlPreview>('/graphql/preview', 'POST', {
		command,
		args,
		graphql_schema: graphqlSchema
	});
}

/** The base URL an openapi document names, fetched without credentials. */
export function previewOpenapi(command: string, openapiDocument: string): Promise<OpenapiPreview> {
	return jsonRequest<OpenapiPreview>('/openapi/preview', 'POST', {
		command,
		openapi_document: openapiDocument
	});
}

/**
 * Imports servers from a payload.
 *
 * @param payload - The import data to send to the server
 * @returns The import result
 */
export function importServers(payload: unknown): Promise<ImportResult> {
	return jsonRequest<ImportResult>('/servers/import', 'POST', payload);
}

// ---- Catalog (browse upstream MCP directories + install) --------------------

/**
 * Lists the available catalog sources.
 *
 * @returns The catalog sources available for browsing.
 */
export function listCatalogSources(): Promise<CatalogSource[]> {
	return request<CatalogSource[]>('/catalog/sources');
}

/**
 * Browses catalog servers with optional filtering and pagination.
 *
 * @param params - Query parameters used to filter and page the catalog results.
 * @param params.source - The catalog source to query.
 * @param params.search - A search term to match against catalog entries.
 * @param params.cursor - The pagination cursor from the previous result page.
 * @param params.limit - The maximum number of results to return.
 * @returns The catalog listing for the requested query.
 */
export function listCatalog(params: {
	source?: string;
	search?: string;
	cursor?: string;
	limit?: number;
}): Promise<CatalogList> {
	const q = new URLSearchParams();
	if (params.source) q.set('source', params.source);
	if (params.search) q.set('search', params.search);
	if (params.cursor) q.set('cursor', params.cursor);
	if (params.limit) q.set('limit', String(params.limit));
	const qs = q.toString();
	return request<CatalogList>(`/catalog/servers${qs ? `?${qs}` : ''}`);
}

/** List a catalog server's selectable versions (latest first; empty if unversioned). */
export function getCatalogVersions(id: string, source = 'official'): Promise<CatalogVersions> {
	const q = new URLSearchParams({ id, source });
	return request<CatalogVersions>(`/catalog/server/versions?${q.toString()}`);
}

/**
 * Resolves a catalog server to its install details and metadata.
 *
 * @param id - The catalog server ID
 * @param source - The catalog source to query
 * @param version - The catalog version to query
 * @returns The catalog server details
 */
export function getCatalogServer(
	id: string,
	source = 'official',
	version = 'latest'
): Promise<CatalogDetail> {
	const q = new URLSearchParams({ id, source, version });
	return request<CatalogDetail>(`/catalog/server?${q.toString()}`);
}

/**
 * Resolves a GitHub repository URL to install drafts (npx or uvx installing from git),
 * in the same shape as a catalog entry so it reviews through the same form.
 *
 * @param url - The repository URL, e.g. `https://github.com/owner/repo`
 * @returns The resolved details
 */
export function getRepositoryDraft(url: string): Promise<CatalogDetail> {
	const q = new URLSearchParams({ url });
	return request<CatalogDetail>(`/catalog/repository?${q.toString()}`);
}

/**
 * Updates a server.
 *
 * @param id - The server ID.
 * @param body - The fields to update.
 * @returns The updated server summary.
 */
export function updateServer(
	id: string,
	body: ServerUpdate
): Promise<ServerSummary> {
	return jsonRequest<ServerSummary>(
		`/servers/${encodeURIComponent(id)}`,
		'PATCH',
		body
	);
}

/**
 * Download the generated `.mcpb` bundle for a local stdio server. The body is a
 * zip, not JSON, and it needs the same bearer header an `<a href>` can't carry.
 */
export async function downloadMcpb(id: string): Promise<Blob> {
	const res = await apiFetch(`/servers/${encodeURIComponent(id)}/mcpb`);
	return res.blob();
}

export function deleteServer(id: string): Promise<void> {
	return request<void>(`/servers/${encodeURIComponent(id)}`, {
		method: 'DELETE'
	});
}

/**
 * Duplicate a server's config into a new, disabled server (fresh id + unique slug).
 * Pass `name` to label the copy; the backend defaults to `"<source> copy"`.
 */
export function cloneServer(id: string, name?: string): Promise<ServerSummary> {
	return jsonRequest<ServerSummary>(
		`/servers/${encodeURIComponent(id)}/clone`,
		'POST',
		name ? { name } : {}
	);
}

/**
 * Begin the interactive upstream-OAuth flow for a remote server. Returns the
 * provider authorization URL the caller should navigate the browser to; the flow
 * finishes at `/api/oauth/callback`, which redirects back to the server page.
 */
export function startOauth(id: string): Promise<{ authorize_url: string }> {
	return request<{ authorize_url: string }>(
		`/servers/${encodeURIComponent(id)}/oauth/authorize`,
		{ method: 'POST' }
	);
}

/** Forget a remote server's stored upstream OAuth tokens (so it can be re-authenticated). */
export function disconnectOauth(id: string): Promise<ServerDetail> {
	return request<ServerDetail>(`/servers/${encodeURIComponent(id)}/oauth/disconnect`, {
		method: 'POST'
	});
}

export function enableServer(id: string): Promise<ServerSummary> {
	return request<ServerSummary>(`/servers/${encodeURIComponent(id)}/enable`, {
		method: 'POST'
	});
}

export function disableServer(id: string): Promise<ServerSummary> {
	return request<ServerSummary>(`/servers/${encodeURIComponent(id)}/disable`, {
		method: 'POST'
	});
}

export function retryServer(id: string): Promise<ServerSummary> {
	return request<ServerSummary>(`/servers/${encodeURIComponent(id)}/retry`, {
		method: 'POST'
	});
}

/**
 * Restart an enabled server: the bridge is stopped and re-activated, so discovery
 * re-runs and every surface (MCP, REST, the group hub) picks up the upstream's
 * current tools. Desired state is untouched — no config change, no re-hash. Works
 * from `running`, `idle`, `starting`, and `failed`; a disabled server is a 409
 * (Start is the action for it).
 */
export function restartServer(id: string): Promise<ServerSummary> {
	return request<ServerSummary>(`/servers/${encodeURIComponent(id)}/restart`, {
		method: 'POST'
	});
}

/**
 * Delete a local (npx/uvx/command) server's cache directory — its XDG_CACHE_HOME and
 * Playwright browsers under `<data_dir>/cache/<id>`. An enabled server is stopped first
 * and restarted after (through the same restart primitive); a disabled one is only
 * cleared. Any other runner is a 400.
 */
export function clearServerCache(id: string): Promise<CacheClearResult> {
	return request<CacheClearResult>(`/servers/${encodeURIComponent(id)}/cache/clear`, {
		method: 'POST'
	});
}

/**
 * Invoke one tool on a running server's bridge (the tool playground). Runs over
 * the control plane, so no data-plane bearer token is needed. A tool's own
 * failure comes back as `is_error` in a 200; a stopped server is a 409 and an
 * unreachable bridge a 502.
 */
export function callServerTool(
	id: string,
	tool: string,
	args: Record<string, unknown>
): Promise<ToolCallResult> {
	return jsonRequest<ToolCallResult>(
		`/servers/${encodeURIComponent(id)}/tools/${encodeURIComponent(tool)}/call`,
		'POST',
		{ arguments: args }
	);
}

export interface LogStreamHandlers {
	onOpen?: () => void;
	onLine: (line: string) => void;
	onInfo: () => void; // the server isn't running — the stream is done
}

/**
 * Stream a server's live logs over fetch + ReadableStream so the `Authorization`
 * header is sent (`EventSource` can't set headers). Parses SSE `data:` frames into
 * the same `{ type, line }` objects the backend emits. Resolves when the stream
 * ends; rejects on transport/abort errors. Pass an `AbortSignal` to stop it.
 */
export async function streamLogs(
	id: string,
	handlers: LogStreamHandlers,
	signal: AbortSignal
): Promise<void> {
	const path = `/servers/${encodeURIComponent(id)}/logs`;
	const res = await apiFetch(path, { headers: { accept: 'text/event-stream' }, signal });
	if (!res.body) throw new ApiError(res.status, '', `${BASE}${path}`);
	handlers.onOpen?.();

	const reader = res.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	for (;;) {
		const { done, value } = await reader.read();
		if (done) return;
		buffer += decoder.decode(value, { stream: true });
		// SSE frames are separated by a blank line. Accept LF and CRLF endings
		// (FastAPI / sse-starlette and some proxies emit CRLF); otherwise the parser
		// would never find a boundary and the buffer would grow without bound.
		for (;;) {
			const boundary = /\r\n\r\n|\n\n/.exec(buffer);
			if (!boundary) break;
			const frame = buffer.slice(0, boundary.index);
			buffer = buffer.slice(boundary.index + boundary[0].length);
			const data = frame
				.split(/\r\n|\n/)
				.filter((l) => l.startsWith('data:'))
				.map((l) => l.slice(5).trim())
				.join('\n');
			if (!data) continue;
			let ev: { type?: string; line?: string };
			try {
				ev = JSON.parse(data);
			} catch {
				continue;
			}
			if (ev.type === 'info') {
				handlers.onInfo();
				return;
			}
			if (typeof ev.line === 'string') handlers.onLine(ev.line);
		}
	}
}

// ---- Auth & settings (M5) ---------------------------------------------------

/** Whether control-plane auth is enforced, and whether this client is authenticated.
 * Public endpoint — the layout calls it to decide whether to redirect to /login. */
export function getAuthStatus(withToken = true): Promise<AuthStatus> {
	const status = request<AuthStatus>('/auth/status', undefined, withToken).then((s) => {
		csrf = s.csrf ?? null;
		return s;
	});
	if (!firstStatus) {
		const first: Promise<unknown> = status.catch(() => {
			// A failed first status must not stick: the next request asks again.
			if (firstStatus === first) firstStatus = null;
		});
		firstStatus = first;
	}
	return status;
}

/** End this browser's Sign-in session (if any), then forget its CSRF value. Both calls
 * go without the login token: a request carrying one is judged on the token alone, so
 * status would hand out no CSRF value for the session. A failure throws and keeps the
 * CSRF value, so the caller can retry. Then it leaves: to the provider's logout URL
 * when the backend returns one (ending the provider session too), else to /login. */
export async function logout(
	go: (url: string) => void = (url) => window.location.assign(url)
): Promise<void> {
	await getAuthStatus(false);
	const res = await request<{ provider_logout_url?: string | null }>(
		'/auth/logout',
		{ method: 'POST' },
		false
	);
	csrf = null;
	if (res?.provider_logout_url) go(res.provider_logout_url);
	else await goto('/login');
}

/** Current security settings: bind mode, allowed hosts, default auth provider. */
export function getSettings(): Promise<SettingsInfo> {
	return request<SettingsInfo>('/settings');
}

/**
 * Patch any subset of the security settings. The backend returns the full,
 * updated settings object; a 400 (invalid bind_mode / default_auth_provider)
 * surfaces via `ApiError`.
 */
export function updateSettings(
	patch: Partial<SettingsInfo>
): Promise<SettingsInfo> {
	return jsonRequest<SettingsInfo>('/settings', 'PATCH', patch);
}

/** List access tokens. Each is identified by prefix only (no plaintext). */
export function listTokens(): Promise<TokenInfo[]> {
	return request<TokenInfo[]>('/tokens');
}

/**
 * Mint a new bearer token. The response carries the full plaintext `token`
 * exactly once — it is never retrievable again, so the caller must surface it
 * immediately. `scope` is `'all'` (every bearer-protected server) or a server id
 * to restrict the token to that one server.
 */
export function createToken(name: string, scope = 'all'): Promise<TokenCreated> {
	return jsonRequest<TokenCreated>('/tokens', 'POST', { name, scope });
}

/** Revoke a token by id. */
export function deleteToken(id: string): Promise<void> {
	return request<void>(`/tokens/${encodeURIComponent(id)}`, {
		method: 'DELETE'
	});
}

// ---- Users (multi-user control plane; admin-only) ---------------------------

/** List control-plane users with their server/token counts. */
export function listUsers(): Promise<UserInfo[]> {
	return request<UserInfo[]>('/users');
}

/** Create a user. `local_runners` gates code-executing runners (npx/uvx/command/docker). */
export function createUser(
	name: string,
	role: 'admin' | 'member',
	local_runners: boolean
): Promise<UserInfo> {
	return jsonRequest<UserInfo>('/users', 'POST', { name, role, local_runners });
}

/** Patch a user's name, role, local-runner permission, or control-MCP access. A 409 surfaces the
 * last-admin guard (no other admin login would remain). */
export function updateUser(
	id: string,
	patch: Partial<Pick<UserInfo, 'name' | 'role' | 'local_runners' | 'control_mcp' | 'active'>>
): Promise<UserInfo> {
	return jsonRequest<UserInfo>(`/users/${encodeURIComponent(id)}`, 'PATCH', patch);
}

/** Delete a user (revokes all their tokens). 409 while they still own servers. */
export function deleteUser(id: string): Promise<void> {
	return request<void>(`/users/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** Clear a user's Linked identity and end their Sign-in sessions; their next SSO sign-in
 * matches by name again. */
export function unlinkUserIdentity(id: string): Promise<void> {
	return request<void>(`/users/${encodeURIComponent(id)}/identity`, { method: 'DELETE' });
}

/** Mint a login (control) token for a user — plaintext returned exactly once. */
export function mintUserCredential(id: string): Promise<UserCredential> {
	return jsonRequest<UserCredential>(`/users/${encodeURIComponent(id)}/credentials`, 'POST', {});
}

// ---- Groups (the /g/<name> registry) ----------------------------------------

/** List the configured groups (each with its members and copyable /g/<name>/mcp URL). */
export function listGroups(): Promise<GroupInfo[]> {
	return request<GroupInfo[]>('/groups');
}

/** Create or replace a group. `members` is `'*'` (every registered server) or an
 * ordered list of server ids. A 400 surfaces an unknown member id or a bad name. */
export function putGroup(name: string, members: GroupMembers): Promise<GroupInfo> {
	// Guard an empty name here: an empty path segment would resolve to `/groups/` and
	// hit the collection route (405/unexpected) instead of failing clearly.
	if (!name) return Promise.reject(new Error('group name is required'));
	return jsonRequest<GroupInfo>(`/groups/${encodeURIComponent(name)}`, 'PUT', { members });
}

/**
 * Restart a group: every enabled member is bounced through the same per-server
 * restart, so the bundle re-reads their tools. The result names which members were
 * restarted and which were skipped (disabled ones have no bridge to bounce).
 */
export function restartGroup(name: string): Promise<GroupRestartResult> {
	if (!name) return Promise.reject(new Error('group name is required'));
	return request<GroupRestartResult>(`/groups/${encodeURIComponent(name)}/restart`, {
		method: 'POST'
	});
}

/** Delete a group by name. */
export function deleteGroup(name: string): Promise<void> {
	if (!name) return Promise.reject(new Error('group name is required'));
	return request<void>(`/groups/${encodeURIComponent(name)}`, { method: 'DELETE' });
}
