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
	HealthResponse,
	ImportResult,
	ServerCreate,
	ServerDetail,
	ServerSummary,
	ServerUpdate,
	SettingsInfo,
	TokenCreated,
	TokenInfo
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const url = `${BASE}${path}`;
	const token = getToken();
	const res = await fetch(url, {
		...init,
		headers: {
			accept: 'application/json',
			...(token ? { authorization: `Bearer ${token}` } : {}),
			...init?.headers
		}
	});

	if (!res.ok) {
		// A 401 means the token is missing or stale: drop it and bounce to /login
		// (unless already there) so the SPA doesn't sit on a dead session.
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

export function getServer(id: string): Promise<ServerDetail> {
	return request<ServerDetail>(`/servers/${encodeURIComponent(id)}`);
}

export function createServer(body: ServerCreate): Promise<ServerSummary> {
	return jsonRequest<ServerSummary>('/servers', 'POST', body);
}

export function importServers(payload: unknown): Promise<ImportResult> {
	return jsonRequest<ImportResult>('/servers/import', 'POST', payload);
}

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
	const url = `${BASE}/servers/${encodeURIComponent(id)}/logs`;
	const token = getToken();
	const res = await fetch(url, {
		headers: {
			accept: 'text/event-stream',
			...(token ? { authorization: `Bearer ${token}` } : {})
		},
		signal
	});
	if (res.status === 401) {
		clearToken();
		if (typeof window !== 'undefined' && window.location.pathname !== '/login') void goto('/login');
		throw new ApiError(401, '', url);
	}
	if (!res.ok || !res.body) throw new ApiError(res.status, '', url);
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
export function getAuthStatus(): Promise<AuthStatus> {
	return request<AuthStatus>('/auth/status');
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
