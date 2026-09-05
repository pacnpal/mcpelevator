import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { UsageBand, UsagePoint } from '$lib/types';
import UsageSparklines from './UsageSparklines.svelte';

// The per-facet chart is LayerChart's; what this file guards is the faceting —
// one panel per server, each labelled with its own total. The shared y domain and
// the point mapping are pure (`facetPeak` / `toFacetPoints`) and tested directly
// in `usage.test.ts`.

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function render(series: UsagePoint[], bands: UsageBand[], bucketSeconds = 3600) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(UsageSparklines, { target: host, props: { series, bands, bucketSeconds } });
	return host;
}

function point(bucket: string): UsagePoint {
	return { bucket, calls: 0, other: 0 };
}

function band(slug: string, points: number[], server_id: string | null = slug): UsageBand {
	return { server_id, slug, name: slug, points };
}

afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	component = null;
	host = null;
});

describe('UsageSparklines', () => {
	const series = [point('2026-09-01T10:00:00Z'), point('2026-09-01T11:00:00Z')];

	it('renders one facet per server, each with its own total', () => {
		const target = render(series, [band('alpha', [3, 1]), band('beta', [0, 2])]);
		const facets = target.querySelectorAll('[data-testid="usage-facet"]');
		expect(facets).toHaveLength(2);
		expect(facets[0].textContent).toContain('alpha');
		expect([...facets].map((f) => f.getAttribute('data-total'))).toEqual(['4', '2']);
	});

	it('draws marks inside each facet', () => {
		const target = render(series, [band('alpha', [3, 1])]);
		const facet = target.querySelector('[data-slug="alpha"]');
		expect(facet?.querySelector('.lc-root-container')).not.toBeNull();
		expect(facet?.querySelectorAll('rect').length).toBeGreaterThan(0);
	});

	it('renders an all-quiet facet without failing', () => {
		const target = render(series, [band('alpha', [0, 0])]);
		expect(target.querySelectorAll('[data-testid="usage-facet"]')).toHaveLength(1);
		expect(target.querySelector('[data-slug="alpha"] .lc-root-container')).not.toBeNull();
	});

	it('renders nothing when there is nothing to split', () => {
		const target = render(series, []);
		expect(target.querySelectorAll('[data-testid="usage-facet"]')).toHaveLength(0);
	});
});
