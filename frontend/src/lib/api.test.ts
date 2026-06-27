import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { goto } from '$app/navigation';

import { ApiError, cloneServer, getHealth, streamLogs } from './api';
import { getToken, setToken } from './auth';

// api.ts imports goto at module load; mock the SvelteKit module so the 401 path
// can be asserted without a real router.
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

function stubFetch(status: number, body: unknown) {
	const res = {
		ok: status >= 200 && status < 300,
		status,
		json: async () => body,
		text: async () => JSON.stringify(body)
	};
	const fn = vi.fn().mockResolvedValue(res);
	vi.stubGlobal('fetch', fn);
	return fn;
}

describe('api request()', () => {
	beforeEach(() => {
		localStorage.clear();
		vi.clearAllMocks();
	});
	afterEach(() => vi.unstubAllGlobals());

	it('attaches the bearer header when a token is set', async () => {
		setToken('mcpe_secret');
		const fetchMock = stubFetch(200, { status: 'ok', version: '1' });
		await getHealth();
		const init = fetchMock.mock.calls[0][1] as RequestInit;
		expect((init.headers as Record<string, string>).authorization).toBe('Bearer mcpe_secret');
	});

	it('omits the bearer header when no token is set', async () => {
		const fetchMock = stubFetch(200, { status: 'ok', version: '1' });
		await getHealth();
		const init = fetchMock.mock.calls[0][1] as RequestInit;
		expect((init.headers as Record<string, string>).authorization).toBeUndefined();
	});

	it('on 401 clears the token and redirects to /login', async () => {
		setToken('mcpe_stale');
		stubFetch(401, { detail: 'control-plane auth required' });
		await expect(getHealth()).rejects.toBeInstanceOf(ApiError);
		expect(getToken()).toBeNull();
		expect(goto).toHaveBeenCalledWith('/login');
	});

	it('cloneServer POSTs to the clone endpoint with an empty body by default', async () => {
		const fetchMock = stubFetch(201, { id: 'new', slug: 'memory-2' });
		await cloneServer('src-id');
		const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(url).toBe('/api/servers/src-id/clone');
		expect(init.method).toBe('POST');
		expect(init.body).toBe('{}');
	});

	it('cloneServer forwards a custom name in the body', async () => {
		const fetchMock = stubFetch(201, { id: 'new', slug: 'staging' });
		await cloneServer('src-id', 'Memory staging');
		const init = fetchMock.mock.calls[0][1] as RequestInit;
		expect(init.body).toBe(JSON.stringify({ name: 'Memory staging' }));
	});

	it('parses both CRLF- and LF-framed SSE log frames', async () => {
		const enc = new TextEncoder();
		const body = new ReadableStream<Uint8Array>({
			start(controller) {
				controller.enqueue(enc.encode('data: {"line":"a"}\r\n\r\n')); // CRLF
				controller.enqueue(enc.encode('data: {"line":"b"}\n\n')); // LF
				controller.close();
			}
		});
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, body }));
		const lines: string[] = [];
		await streamLogs(
			'srv',
			{ onLine: (l) => lines.push(l), onInfo: () => {} },
			new AbortController().signal
		);
		expect(lines).toEqual(['a', 'b']);
	});
});
