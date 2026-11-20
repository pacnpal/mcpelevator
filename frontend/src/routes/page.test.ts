import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { page } from './login/fakePage.svelte';
import Dashboard from './+page.svelte';

const nav = vi.hoisted(() => ({ goto: vi.fn(), pending: [] as (() => void)[] }));

vi.mock('$app/state', async () => ({ page: (await import('./login/fakePage.svelte')).page }));
vi.mock('$app/navigation', () => ({ goto: nav.goto }));
vi.mock('#lib/api.js', () => {
	const server = (name: string, state: string) => ({
		id: name,
		slug: name,
		name,
		runner: 'npx',
		enabled: state !== 'stopped',
		state,
		startup_status: null,
		transports: { mcp_http: true, rest_openapi: false },
		urls: { mcp: `http://localhost/s/${name}/mcp`, rest: null },
		auth: 'none',
		last_error: null,
		pid: null,
		port: null,
		tools_count: 0,
		tool_names: [],
		owner_id: null,
		owner_name: null
	});
	return {
		errorMessage: () => 'error',
		listServers: async () => [server('files', 'stopped'), server('fetch', 'running')],
		getAuthStatus: async () => ({ user: null }),
		getInstanceUsage: async () => ({ servers: [] })
	};
});

let dispose: (() => void) | undefined;
afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	nav.goto.mockReset();
	nav.pending = [];
});

async function render(url: string) {
	page.url = new URL(url);
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(Dashboard, { target });
	dispose = () => unmount(component);
	await new Promise((r) => setTimeout(r, 0));
	flushSync();
	return {
		search: () => target.querySelector<HTMLInputElement>('#server-search')!,
		chip: (label: string) =>
			[...target.querySelectorAll<HTMLButtonElement>('[aria-label="Filter by state"] button')].find(
				(b) => b.textContent?.includes(label)
			)!,
		text: () => target.textContent ?? ''
	};
}

describe('dashboard filters follow the URL', () => {
	it('clears the search text and the chips when navigation drops the query', async () => {
		const view = await render('http://localhost/?q=fil&state=stopped');
		expect(view.search().value).toBe('fil');
		expect(view.chip('Stopped').getAttribute('aria-pressed')).toBe('true');
		expect(view.text()).toContain('1 of 2 servers');

		// The home logo: same route, no query.
		page.url = new URL('http://localhost/');
		flushSync();
		expect(view.search().value).toBe('');
		expect(view.chip('Stopped').getAttribute('aria-pressed')).toBe('false');
		expect(view.text()).not.toContain('of 2 servers');
	});

	it('keeps typed text while its own navigations are in flight', async () => {
		nav.goto.mockImplementation(() => new Promise<void>((r) => nav.pending.push(r)));
		const view = await render('http://localhost/');
		const input = view.search();
		input.focus();
		input.value = 'f';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		input.value = 'fe';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		expect(nav.goto).toHaveBeenLastCalledWith('/?q=fe', { replace: true, reset: false });

		// The first keystroke's navigation lands after the second was typed.
		page.url = new URL('http://localhost/?q=f');
		flushSync();
		expect(input.value).toBe('fe');
		expect(document.activeElement).toBe(input);

		page.url = new URL('http://localhost/?q=fe');
		nav.pending.forEach((r) => r());
		await new Promise((r) => setTimeout(r, 0));
		flushSync();
		expect(input.value).toBe('fe');

		// With nothing of ours in flight, an outside navigation wins.
		page.url = new URL('http://localhost/?q=other');
		flushSync();
		expect(input.value).toBe('other');
	});
});
