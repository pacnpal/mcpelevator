import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { UsagePoint } from '$lib/types';
import UsageChart from './UsageChart.svelte';

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function render(series: UsagePoint[], bucketSeconds = 3600, mode: 'bars' | 'line' = 'bars') {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(UsageChart, { target: host, props: { series, bucketSeconds, mode } });
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
	it('renders one bar per bucket, quiet ones included', () => {
		// The series is dense by contract, so an empty bucket must still occupy its
		// slot — otherwise a gap in traffic would silently compress the timeline.
		const target = render([
			point('2026-09-01T10:00:00Z', 3),
			point('2026-09-01T11:00:00Z', 0),
			point('2026-09-01T12:00:00Z', 1)
		]);
		const bars = target.querySelectorAll('[data-testid="usage-bar"]');
		expect(bars).toHaveLength(3);
		expect([...bars].map((b) => b.getAttribute('data-calls'))).toEqual(['3', '0', '1']);
	});

	it('scales bars against the busiest bucket', () => {
		const target = render([point('2026-09-01T10:00:00Z', 10), point('2026-09-01T11:00:00Z', 5)]);
		// The mark sits inside the width-capped column wrapper (columns are capped so a
		// short window leaves air between marks rather than drawing slabs).
		const [tallest, half] = [...target.querySelectorAll('[data-testid="usage-bar"]')].map(
			(bar) => (bar.querySelector(':scope > div > div') as HTMLElement).style.height
		);
		expect(tallest).toContain('100%');
		expect(half).toContain('50%');
	});

	it('summarizes the window for screen readers', () => {
		const target = render([point('2026-09-01T10:00:00Z', 2), point('2026-09-01T11:00:00Z', 4)]);
		expect(target.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(
			'6 tool calls across 2 hourly buckets'
		);
	});

	it('labels daily buckets as dates rather than times', () => {
		const target = render([point('2026-09-01T00:00:00Z', 1)], 86400);
		expect(target.querySelector('[role="img"]')?.getAttribute('aria-label')).toContain(
			'daily buckets'
		);
		// The caption shows the window's edges; a daily bucket must not read as a clock time.
		expect(target.querySelector('figcaption')?.textContent).not.toMatch(/\d:\d\d/);
	});

	it('draws a polyline instead of bars in line mode', () => {
		const target = render(
			[point('2026-09-01T10:00:00Z', 4, 1), point('2026-09-01T11:00:00Z', 0, 2)],
			3600,
			'line'
		);
		expect(target.querySelector('[data-testid="usage-line"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="usage-bar"]')).toBeNull();
		// Both quantities are plotted: tool calls and the total including other traffic.
		expect(target.querySelectorAll('polyline').length).toBe(3);
	});

	it('keeps the same summary in either style', () => {
		const series = [point('2026-09-01T10:00:00Z', 2), point('2026-09-01T11:00:00Z', 4)];
		const bars = render(series).querySelector('[role="img"]')?.getAttribute('aria-label');
		if (component) unmount(component);
		host?.remove();
		const line = render(series, 3600, 'line')
			.querySelector('[role="img"]')
			?.getAttribute('aria-label');
		expect(line).toBe(bars);
	});

	it('renders an all-quiet window without dividing by zero', () => {
		const target = render([point('2026-09-01T10:00:00Z', 0), point('2026-09-01T11:00:00Z', 0)]);
		const bars = target.querySelectorAll('[data-testid="usage-bar"]');
		expect(bars).toHaveLength(2);
		// Only the muted "other" baseline is drawn — no accent bar for zero calls.
		expect(target.innerHTML).not.toContain('var(--color-accent)');
	});
});
