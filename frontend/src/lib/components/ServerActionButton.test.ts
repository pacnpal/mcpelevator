import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ServerSummary } from '$lib/types';
import ServerActionButton from './ServerActionButton.svelte';

const api = vi.hoisted(() => ({
	disableServer: vi.fn(),
	enableServer: vi.fn(),
	retryServer: vi.fn(),
	errorMessage: vi.fn((error: unknown) => (error instanceof Error ? error.message : 'error'))
}));

vi.mock('$lib/api', () => api);

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
		pid: 1,
		port: 9000,
		tools_count: 0,
		...overrides
	};
}

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof ServerActionButton>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(ServerActionButton, { target, props });
	flushSync();
	dispose = () => void unmount(component);
	const el = target.querySelector('button');
	if (!(el instanceof HTMLButtonElement)) throw new Error('no button rendered');
	return el;
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('ServerActionButton', () => {
	it('stops a running server', async () => {
		const next = summary({ state: 'stopping' });
		api.disableServer.mockResolvedValue(next);
		const onchange = vi.fn();
		const el = render({ server: summary(), onchange });

		expect(el.textContent?.trim()).toBe('Stop');
		el.click();
		await vi.waitFor(() => expect(onchange).toHaveBeenCalledWith(next));
		expect(api.disableServer).toHaveBeenCalledWith('srv-1');
	});

	it('starts a stopped server', async () => {
		api.enableServer.mockResolvedValue(summary({ state: 'starting' }));
		const el = render({ server: summary({ enabled: false, state: 'stopped' }) });

		expect(el.textContent?.trim()).toBe('Start');
		el.click();
		await vi.waitFor(() => expect(api.enableServer).toHaveBeenCalledWith('srv-1'));
		expect(api.disableServer).not.toHaveBeenCalled();
	});

	it('retries an enabled terminal failure', async () => {
		api.retryServer.mockResolvedValue(summary({ state: 'starting' }));
		const el = render({ server: summary({ state: 'unhealthy' }) });

		expect(el.textContent?.trim()).toBe('Retry');
		el.click();
		await vi.waitFor(() => expect(api.retryServer).toHaveBeenCalledWith('srv-1'));
	});

	it('reports a failure to the caller and stays clickable', async () => {
		api.disableServer.mockRejectedValue(new Error('supervisor is busy'));
		const onerror = vi.fn();
		const el = render({ server: summary(), onerror });

		el.click();
		await vi.waitFor(() => expect(onerror).toHaveBeenCalledWith('supervisor is busy'));
		await vi.waitFor(() => expect(el.disabled).toBe(false));
	});

	it('does nothing while the caller has it disabled', () => {
		const el = render({ server: summary(), disabled: true });
		expect(el.disabled).toBe(true);
		el.click();
		expect(api.disableServer).not.toHaveBeenCalled();
	});
});
