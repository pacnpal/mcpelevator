// Typed fetch helpers for the mcpelevator backend.
//
// All requests target the same-origin `/api` base. In dev, Vite proxies this
// to the FastAPI backend (see vite.config.ts); in production the backend
// serves both the API and the built SPA, so the path is identical.
//
// Every helper throws `ApiError` on a non-2xx response, carrying the status
// and the response body text so callers can surface a useful message.

import type {
	HealthResponse,
	ImportResult,
	ServerCreate,
	ServerDetail,
	ServerSummary,
	ServerUpdate
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
	const res = await fetch(url, {
		headers: { accept: 'application/json', ...init?.headers },
		...init
	});

	if (!res.ok) {
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

/** URL for a server's live log SSE stream (consumed via `EventSource`). */
export function logStreamUrl(id: string): string {
	return `${BASE}/servers/${encodeURIComponent(id)}/logs`;
}
