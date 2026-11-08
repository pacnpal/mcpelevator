import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { StartupStatus } from '#lib/types.js';
import LogViewer from './LogViewer.svelte';

const api = vi.hoisted(() => ({ streamLogs: vi.fn() }));

vi.mock('#lib/api.js', () => ({
	streamLogs: api.streamLogs,
	ApiError: class ApiError extends Error {
		constructor(readonly status: number) {
			super('API error');
		}
	}
}));

function status(activationStartedAt: string): StartupStatus {
	return {
		phase: 'setup',
		attempt: 1,
		max_attempts: 5,
		activation_started_at: activationStartedAt,
		deadline_at: null,
		next_retry_at: null,
		message: null
	};
}

let dispose: (() => void) | undefined;

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.useRealTimers();
	vi.clearAllMocks();
});

describe('LogViewer activation stream', () => {
	it('keeps a terminal activation backlog visible and bounded after the stream closes', async () => {
		api.streamLogs.mockImplementationOnce(async (_id, handlers) => {
			await Promise.resolve();
			handlers.onOpen?.();
			for (let i = 0; i < 1005; i += 1) handlers.onLine(`line ${i}`);
			handlers.onInfo();
		});

		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LogViewer, {
			target,
			props: { serverId: 'srv-1', serverState: 'failed' }
		});
		dispose = () => void unmount(component);
		flushSync();
		await Promise.resolve();
		flushSync();

		const rendered = target.querySelectorAll('.whitespace-pre-wrap');
		expect(rendered).toHaveLength(1000);
		expect(rendered[0]?.textContent).toBe('line 5');
		expect(rendered[999]?.textContent).toBe('line 1004');
	});

	it('retries an early not-running response during active setup and replaces replayed lines', async () => {
		vi.useFakeTimers();
		api.streamLogs
			.mockImplementationOnce(async (_id, handlers) => {
				await Promise.resolve();
				handlers.onOpen?.();
				handlers.onLine('old backlog');
				handlers.onInfo();
			})
			.mockImplementationOnce(async (_id, handlers, signal: AbortSignal) => {
				await Promise.resolve();
				handlers.onOpen?.();
				handlers.onLine('current backlog');
				await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()));
			});

		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LogViewer, {
			target,
			props: {
				serverId: 'srv-1',
				serverState: 'stopped',
				startupStatus: status('2026-07-15T12:00:00Z')
			}
		});
		dispose = () => void unmount(component);
		flushSync();
		expect(api.streamLogs).toHaveBeenCalledTimes(1);

		await Promise.resolve();
		await vi.advanceTimersByTimeAsync(1000);
		flushSync();
		expect(api.streamLogs).toHaveBeenCalledTimes(2);
		expect(target.textContent).toContain('current backlog');
		expect(target.textContent).not.toContain('old backlog');
	});

	it('resets and reconnects when the activation changes', async () => {
		api.streamLogs
			.mockImplementationOnce(async (_id, handlers, signal: AbortSignal) => {
				await Promise.resolve();
				handlers.onOpen?.();
				handlers.onLine('first activation');
				await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()));
			})
			.mockImplementationOnce(async (_id, handlers, signal: AbortSignal) => {
				await Promise.resolve();
				handlers.onOpen?.();
				handlers.onLine('second activation');
				await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()));
			});

		const props = $state({
			serverId: 'srv-1',
			serverState: 'stopped' as const,
			startupStatus: status('2026-07-15T12:00:00Z')
		});
		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LogViewer, { target, props });
		dispose = () => void unmount(component);
		flushSync();
		await Promise.resolve();
		flushSync();
		expect(target.textContent).toContain('first activation');

		props.startupStatus = status('2026-07-15T12:05:00Z');
		flushSync();
		await Promise.resolve();
		flushSync();
		expect(api.streamLogs).toHaveBeenCalledTimes(2);
		expect(target.textContent).toContain('second activation');
		expect(target.textContent).not.toContain('first activation');
	});

	it('pause drops the live stream and resume reopens it', async () => {
		api.streamLogs.mockImplementation(async (_id, handlers, signal: AbortSignal) => {
			await Promise.resolve();
			handlers.onOpen?.();
			handlers.onLine('live output');
			await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()));
		});

		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LogViewer, {
			target,
			props: { serverId: 'srv-1', serverState: 'running' as const }
		});
		dispose = () => void unmount(component);
		flushSync();
		await Promise.resolve();
		flushSync();
		expect(api.streamLogs).toHaveBeenCalledTimes(1);
		expect(target.textContent).toContain('live output');

		const button = (label: string) =>
			[...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);

		button('Pause')!.click();
		flushSync();
		await Promise.resolve();
		flushSync();
		// Stream aborted, not reopened, and the backlog stays frozen on screen.
		expect(api.streamLogs).toHaveBeenCalledTimes(1);
		expect(target.textContent).toContain('paused');
		expect(target.textContent).toContain('live output');

		button('Resume')!.click();
		flushSync();
		await Promise.resolve();
		flushSync();
		expect(api.streamLogs).toHaveBeenCalledTimes(2);
	});

	it('filter hides non-matching lines and reports no matches', async () => {
		api.streamLogs.mockImplementation(async (_id, handlers, signal: AbortSignal) => {
			await Promise.resolve();
			handlers.onOpen?.();
			handlers.onLine('upstream_error: no such host');
			handlers.onLine('INFO: 200 OK');
			await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()));
		});

		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LogViewer, {
			target,
			props: { serverId: 'srv-1', serverState: 'running' as const }
		});
		dispose = () => void unmount(component);
		flushSync();
		await Promise.resolve();
		flushSync();

		const input = target.querySelector('input[type="search"]') as HTMLInputElement;
		input.value = 'error';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		const shown = [...target.querySelectorAll('.whitespace-pre-wrap')].map((n) => n.textContent);
		expect(shown).toEqual(['upstream_error: no such host']);

		input.value = 'nomatch-zzz';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		expect(target.querySelectorAll('.whitespace-pre-wrap')).toHaveLength(0);
		expect(target.textContent).toContain('No lines match');
	});

	it('keeps one stream open while the page polls in fresh but equal props', async () => {
		// The server page re-fetches the server every second during startup and passes
		// each response down. Equal values must not abort and reopen the stream: on a
		// busy instance the reopened request never answers before the next poll, so the
		// pane sits on "connecting" with nothing in it.
		api.streamLogs.mockImplementation(async (_id, handlers, signal: AbortSignal) => {
			await Promise.resolve();
			handlers.onOpen?.();
			handlers.onLine('setup output');
			await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve()));
		});

		let server = $state({
			id: 'srv-1',
			state: 'starting' as const,
			startup_status: status('2026-07-15T12:00:00Z')
		});
		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(LogViewer, {
			target,
			props: {
				get serverId() {
					return server.id;
				},
				get serverState() {
					return server.state;
				},
				get startupStatus() {
					return server.startup_status;
				}
			}
		});
		dispose = () => void unmount(component);
		flushSync();
		await Promise.resolve();
		flushSync();

		for (let poll = 0; poll < 3; poll += 1) {
			server = {
				id: 'srv-1',
				state: 'starting',
				startup_status: status('2026-07-15T12:00:00Z')
			};
			flushSync();
			await Promise.resolve();
			flushSync();
		}
		expect(api.streamLogs).toHaveBeenCalledTimes(1);
		expect(target.textContent).toContain('setup output');
	});
});
