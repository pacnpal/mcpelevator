import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { UsageHour } from '$lib/types';
import { HEATMAP_DAYS } from '$lib/usage';
import UsageHeatmap from './UsageHeatmap.svelte';

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function render(hourly: UsageHour[]) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(UsageHeatmap, { target: host, props: { hourly } });
	return host;
}

afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	component = null;
	host = null;
});

describe('UsageHeatmap', () => {
	it('renders the full weekday x hour grid even with no data', () => {
		const target = render([]);
		expect(target.querySelectorAll('[data-testid="heatmap-cell"]')).toHaveLength(7 * 24);
	});

	it('lights the cell for the busiest local hour', () => {
		const iso = '2026-09-02T14:00:00Z';
		const local = new Date(iso);
		const target = render([{ bucket: iso, calls: 5 }]);
		const cells = target.querySelectorAll('[data-testid="heatmap-cell"]');
		const index = HEATMAP_DAYS.indexOf(local.getDay()) * 24 + local.getHours();
		expect(cells[index].getAttribute('data-level')).toBe('4');
		// Every other cell stays at the empty step, not the lightest data step.
		const lit = [...cells].filter((c) => c.getAttribute('data-level') !== '0');
		expect(lit).toHaveLength(1);
	});

	it('describes the grid for screen readers', () => {
		const target = render([{ bucket: '2026-09-02T14:00:00Z', calls: 5 }]);
		const label = target.querySelector('[role="img"]')?.getAttribute('aria-label') ?? '';
		expect(label).toContain('5 tool calls');
		expect(label).toContain('busiest hour 5');
	});

	it('titles a cell with its weekday, hour range and count', () => {
		const iso = '2026-09-02T14:00:00Z';
		const local = new Date(iso);
		const target = render([{ bucket: iso, calls: 2 }]);
		const index = HEATMAP_DAYS.indexOf(local.getDay()) * 24 + local.getHours();
		const title =
			target.querySelectorAll('[data-testid="heatmap-cell"]')[index].getAttribute('title') ?? '';
		expect(title).toMatch(/\d\d:00–\d\d:00/);
		expect(title).toContain('2 calls');
	});

	it('ships a scale legend for the sequential ramp', () => {
		const target = render([]);
		const caption = target.querySelector('figcaption')?.textContent ?? '';
		expect(caption).toContain('less');
		expect(caption).toContain('more');
		expect(caption).toContain('Local time');
	});
});
