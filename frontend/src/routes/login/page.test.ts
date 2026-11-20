import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { page } from './fakePage.svelte';
import LoginPage from './+page.svelte';

const auth = vi.hoisted(() => ({ startSsoSignIn: vi.fn() }));

vi.mock('$app/state', async () => ({ page: (await import('./fakePage.svelte')).page }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('#lib/api.js', () => ({
	errorMessage: () => 'error',
	getAuthStatus: async () => ({
		enforced: true,
		authenticated: false,
		user: null,
		sso: { enabled: true, label: 'Acme ID' },
		csrf: null
	})
}));
vi.mock('#lib/auth.js', () => ({
	clearToken: vi.fn(),
	setToken: vi.fn(),
	startSsoSignIn: auth.startSsoSignIn
}));

let dispose: (() => void) | undefined;
afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
});

describe('login page', () => {
	it('follows URL changes without remounting', async () => {
		page.url = new URL('http://localhost/login?next=/a&sso=error&reason=missing_claim&claim=email');
		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LoginPage, { target });
		dispose = () => unmount(component);
		await new Promise((r) => setTimeout(r, 0));
		flushSync();
		expect(target.querySelector('[role=alert]')?.textContent).toContain('the email claim');

		page.url = new URL('http://localhost/login?next=/b&sso=error&reason=missing_claim&claim=sub');
		flushSync();
		expect(target.querySelector('[role=alert]')?.textContent).toContain('the sub claim');
		target.querySelector<HTMLButtonElement>('button[type=button]')?.click();
		expect(auth.startSsoSignIn).toHaveBeenLastCalledWith('/b');

		page.url = new URL('http://localhost/login?next=/c');
		flushSync();
		expect(target.querySelector('[role=alert]')).toBeNull();
	});
});
