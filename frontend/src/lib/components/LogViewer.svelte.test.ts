import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { StartupStatus } from '$lib/types';
import LogViewer from './LogViewer.svelte';

const api = vi.hoisted(() => ({ streamLogs: vi.fn() }));

vi.mock('$lib/api', () => ({
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
});
