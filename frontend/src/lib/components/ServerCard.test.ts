import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ServerSummary, StartupStatus } from '$lib/types';
import ServerCard from './ServerCard.svelte';

const api = vi.hoisted(() => ({
	cloneServer: vi.fn(),
	deleteServer: vi.fn(),
	disableServer: vi.fn(),
	enableServer: vi.fn(),
	retryServer: vi.fn(),
	errorMessage: vi.fn((error: unknown) => (error instanceof Error ? error.message : 'error'))
}));

vi.mock('$lib/api', () => api);
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

const startup: StartupStatus = {
	phase: 'setup',
	attempt: 2,
	max_attempts: 5,
	activation_started_at: new Date(Date.now() - 5000).toISOString(),
	deadline_at: new Date(Date.now() + 30_000).toISOString(),
	next_retry_at: null,
	message: 'Installing browser dependencies'
};

function summary(overrides: Partial<ServerSummary> = {}): ServerSummary {
	return {
		id: 'srv-1',
		slug: 'demo',
		name: 'Demo',
		runner: 'command',
		enabled: true,
		state: 'running',
		startup_status: null,
		transports: { mcp_http: true, rest_openapi: false },
		urls: { mcp: 'http://localhost/s/demo/mcp', rest: null },
		auth: 'none',
		last_error: null,
		pid: 123,
		port: 9000,
		tools_count: 4,
		...overrides
	};
}

let dispose: (() => void) | undefined;

function render(server: ServerSummary, onchange = vi.fn()) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(ServerCard, { target, props: { server, onchange } });
	flushSync();
	dispose = () => void unmount(component);
	return { target, onchange };
}

function button(target: HTMLElement, label: string): HTMLButtonElement {
	const found = [...target.querySelectorAll('button')].find(
		(el) => el.textContent?.trim() === label
	);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`${label} button not found`);
	return found;
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('ServerCard startup actions', () => {
	it('shows the active phase over stale runtime state and offers Stop', async () => {
		const server = summary({
			state: 'failed',
			startup_status: startup,
			last_error: 'stale failure'
		});
		api.disableServer.mockResolvedValue(summary({ state: 'stopping' }));
		const { target, onchange } = render(server);

		expect(target.textContent).toContain('Running setup');
		expect(target.textContent).toContain('attempt 2/5');
		expect(target.textContent).not.toContain('stale failure');
		button(target, 'Stop').click();

		await vi.waitFor(() => expect(onchange).toHaveBeenCalled());
		expect(api.disableServer).toHaveBeenCalledWith('srv-1');
	});

	it('offers Retry for an enabled terminal failure', async () => {
		const server = summary({ state: 'unhealthy', last_error: 'bridge exited' });
		api.retryServer.mockResolvedValue(summary({ state: 'starting', startup_status: startup }));
		const { target } = render(server);

		expect(target.textContent).toContain('bridge exited');
		button(target, 'Retry').click();
		await vi.waitFor(() => expect(api.retryServer).toHaveBeenCalledWith('srv-1'));
		expect(api.disableServer).not.toHaveBeenCalled();
	});
});
