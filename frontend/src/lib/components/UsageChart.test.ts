import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { UsagePoint } from '$lib/types';
import UsageChart from './UsageChart.svelte';

// The chart itself is LayerChart's; what this file guards is that we hand it the
// right shape and options, and that both modes mount and draw marks. The data
// mapping is pure and tested directly in `usage.test.ts` — asserting on a
// third-party chart's internal SVG would be brittle and would test their code.

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function render(series: UsagePoint[], props: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(UsageChart, {
		target: host,
		props: { series, bucketSeconds: 3600, ...props }
	});
	return host;
}

function point(bucket: string, calls: number, other = 0): UsagePoint {
	return { bucket, calls, other };
}

afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	component = null;
	host = null;
});

describe('UsageChart', () => {
	const series = [
		point('2026-09-01T10:00:00Z', 3, 1),
		point('2026-09-01T11:00:00Z', 0, 2),
		point('2026-09-01T12:00:00Z', 1)
	];

	it('draws bar marks for the window', () => {
		const target = render(series);
		expect(target.querySelector('.lc-root-container')).not.toBeNull();
		expect(target.querySelectorAll('rect').length).toBeGreaterThan(0);
	});

	it('draws paths instead of bars in line mode', () => {
		const target = render(series, { mode: 'line' });
		expect(target.querySelectorAll('path').length).toBeGreaterThan(0);
	});

	it('renders an all-quiet window without failing', () => {
		// Every scale still has to resolve when nothing was called all window.
		const target = render([point('2026-09-01T10:00:00Z', 0), point('2026-09-01T11:00:00Z', 0)]);
		expect(target.querySelector('.lc-root-container')).not.toBeNull();
	});

	it('renders an empty series without failing', () => {
		const target = render([]);
		expect(target.querySelector('.lc-root-container')).not.toBeNull();
	});

	it('applies the requested plot height', () => {
		const target = render(series, { height: 'h-32' });
		expect(target.firstElementChild?.classList.contains('h-32')).toBe(true);
	});
});
