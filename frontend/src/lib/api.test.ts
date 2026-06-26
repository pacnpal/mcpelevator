import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { goto } from '$app/navigation';

import { ApiError, getHealth } from './api';
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
});
