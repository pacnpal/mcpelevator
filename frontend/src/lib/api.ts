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
	ServerDetail,
	ServerSummary
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

export function getHealth(): Promise<HealthResponse> {
	return request<HealthResponse>('/health');
}

export function listServers(): Promise<ServerSummary[]> {
	return request<ServerSummary[]>('/servers');
}

export function getServer(id: string): Promise<ServerDetail> {
	return request<ServerDetail>(`/servers/${encodeURIComponent(id)}`);
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
