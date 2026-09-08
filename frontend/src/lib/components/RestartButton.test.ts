import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ServerSummary } from '$lib/types';
import RestartButton from './RestartButton.svelte';

const api = vi.hoisted(() => ({
	restartServer: vi.fn(),
	restartGroup: vi.fn(),
	errorMessage: vi.fn((error: unknown) => (error instanceof Error ? error.message : 'error'))
}));
const toast = vi.hoisted(() => ({ flashToast: vi.fn() }));

vi.mock('$lib/api', () => api);
vi.mock('$lib/toast.svelte', () => toast);

const summary = {
	id: 'srv-1',
	slug: 'demo',
	name: 'Demo',
	runner: 'command',
	enabled: true,
	state: 'starting',
	startup_status: null,
	transports: { mcp_http: true, rest_openapi: false },
	urls: { mcp: 'http://localhost/s/demo/mcp', rest: null },
	auth: 'none',
	last_error: null,
	pid: null,
	port: null,
	tools_count: 4
} satisfies ServerSummary;

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof RestartButton>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(RestartButton, { target, props });
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

describe('RestartButton', () => {
	it('restarts one server and hands the refreshed summary back', async () => {
		api.restartServer.mockResolvedValue(summary);
		const onrestarted = vi.fn();
		const el = render({ target: { kind: 'server', id: 'srv-1' }, onrestarted });

		el.click();
		await vi.waitFor(() => expect(onrestarted).toHaveBeenCalledWith(summary));
		expect(api.restartServer).toHaveBeenCalledWith('srv-1');
		expect(api.restartGroup).not.toHaveBeenCalled();
	});

	it('restarts a group and reports how many members were bounced', async () => {
		api.restartGroup.mockResolvedValue({ name: 'team', restarted: ['a', 'b'], skipped: [] });
		const el = render({ target: { kind: 'group', name: 'team' } });

		el.click();
		await vi.waitFor(() =>
			expect(toast.flashToast).toHaveBeenCalledWith('Restarting 2 members of team.', 'info')
		);
		expect(api.restartGroup).toHaveBeenCalledWith('team');
	});

	it('says so when a group has no enabled member to bounce', async () => {
		api.restartGroup.mockResolvedValue({ name: 'team', restarted: [], skipped: ['a'] });
		const el = render({ target: { kind: 'group', name: 'team' } });

		el.click();
		await vi.waitFor(() =>
			expect(toast.flashToast).toHaveBeenCalledWith(
				'No enabled members in team — nothing to restart.',
				'info'
			)
		);
	});

	it('reports a failure through the caller when it supplies an error sink', async () => {
		api.restartServer.mockRejectedValue(new Error('server is not running'));
		const onerror = vi.fn();
		const el = render({ target: { kind: 'server', id: 'srv-1' }, onerror });

		el.click();
		await vi.waitFor(() => expect(onerror).toHaveBeenCalledWith('server is not running'));
		expect(toast.flashToast).not.toHaveBeenCalled();
		// Not stuck busy: a failed restart must be retryable.
		await vi.waitFor(() => expect(el.disabled).toBe(false));
	});

	it('falls back to the shared toast when no error sink is given', async () => {
		api.restartServer.mockRejectedValue(new Error('boom'));
		const el = render({ target: { kind: 'server', id: 'srv-1' } });

		el.click();
		await vi.waitFor(() => expect(toast.flashToast).toHaveBeenCalledWith('boom'));
	});

	it('ignores clicks while disabled by the caller', () => {
		const el = render({ target: { kind: 'server', id: 'srv-1' }, disabled: true });
		expect(el.disabled).toBe(true);
		el.click();
		expect(api.restartServer).not.toHaveBeenCalled();
	});

	it('renders as a menu row in the kebab variant', () => {
		const el = render({ target: { kind: 'server', id: 'srv-1' }, variant: 'menuitem' });
		expect(el.getAttribute('role')).toBe('menuitem');
		expect(el.textContent?.trim()).toBe('Restart');
	});
});
