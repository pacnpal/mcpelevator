import { beforeEach, describe, expect, it } from 'vitest';

import { clearToken, getToken, setToken } from './auth';

describe('auth token store', () => {
	beforeEach(() => localStorage.clear());

	it('round-trips set / get / clear', () => {
		expect(getToken()).toBeNull();
		setToken('mcpe_abc');
		expect(getToken()).toBe('mcpe_abc');
		clearToken();
		expect(getToken()).toBeNull();
	});

	it('overwrites a previous token on set', () => {
		setToken('mcpe_one');
		setToken('mcpe_two');
		expect(getToken()).toBe('mcpe_two');
	});
});
