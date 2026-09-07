import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { HealthResponse } from '$lib/types';
import HealthDot from './HealthDot.svelte';

const api = vi.hoisted(() => ({ getHealth: vi.fn() }));

vi.mock('$lib/api', () => api);

let dispose: (() => void) | undefined;

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.useRealTimers();
	vi.clearAllMocks();
});

describe('HealthDot', () => {
	it('reports each recovered backend version to its host', async () => {
		vi.useFakeTimers();
		api.getHealth
			.mockResolvedValueOnce({ status: 'ok', version: '1.5.2' } satisfies HealthResponse)
			.mockResolvedValueOnce({ status: 'ok', version: '1.5.3' } satisfies HealthResponse);
		const onhealth = vi.fn();
		const target = document.createElement('div');
		document.body.append(target);
		const component = mount(HealthDot, { target, props: { intervalMs: 1000, onhealth } });
		dispose = () => void unmount(component);
		flushSync();

		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(onhealth).toHaveBeenLastCalledWith({ status: 'ok', version: '1.5.2' });

		await vi.advanceTimersByTimeAsync(1000);
		flushSync();
		expect(onhealth).toHaveBeenLastCalledWith({ status: 'ok', version: '1.5.3' });
	});
});
