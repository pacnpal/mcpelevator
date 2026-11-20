import { beforeEach, describe, expect, it, vi } from 'vitest';

import { adoptSsoLanding, clearToken, getToken, setToken, startSsoSignIn } from './auth';

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

describe('SSO and a stored login token', () => {
	beforeEach(() => {
		localStorage.clear();
		document.cookie = 'mcpe_sso_landed=; Max-Age=0; Path=/';
	});

	it('starting SSO clears a stale stored token before navigating', () => {
		setToken('mcpe_revoked');
		const go = vi.fn(() => expect(getToken()).toBeNull());
		startSsoSignIn('/server/abc?tab=logs', go);
		expect(go).toHaveBeenCalledWith('/api/auth/oidc/login?next=%2Fserver%2Fabc%3Ftab%3Dlogs');
	});

	it('landing from a successful SSO callback clears a token set meanwhile', () => {
		setToken('mcpe_from_another_tab');
		document.cookie = 'mcpe_sso_landed=1; Path=/';
		adoptSsoLanding();
		expect(getToken()).toBeNull();
		expect(document.cookie).not.toContain('mcpe_sso_landed');
	});

	it('an ordinary page load keeps the stored token', () => {
		setToken('mcpe_keep');
		adoptSsoLanding();
		expect(getToken()).toBe('mcpe_keep');
	});
});
