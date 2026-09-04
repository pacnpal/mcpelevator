import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { UsageBand, UsagePoint } from '$lib/types';
import UsageSparklines from './UsageSparklines.svelte';

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

	it('renders one facet per server with its total', () => {
		const target = render(series, [band('alpha', [3, 1]), band('beta', [0, 2])]);
		const facets = target.querySelectorAll('[data-testid="usage-facet"]');
		expect(facets).toHaveLength(2);
		expect(facets[0].textContent).toContain('alpha');
		expect(facets[0].querySelector('figcaption')?.textContent).toContain('4');
		expect(facets[1].querySelector('figcaption')?.textContent).toContain('2');
	});

	it('shares ONE scale across facets, so panels stay comparable', () => {
		// A per-facet scale would make a server with 1 call look as busy as one with 4.
		const target = render(series, [band('busy', [4, 0]), band('quiet', [1, 0])]);
		const heightOf = (slug: string) =>
			(
				target.querySelector(
					`[data-slug="${slug}"] [data-testid="facet-bar"] > div > div`
				) as HTMLElement
			).style.height;
		expect(heightOf('busy')).toBe('100%');
		expect(heightOf('quiet')).toBe('25%');
	});

	it('draws no mark for an empty bucket', () => {
		const target = render(series, [band('alpha', [2, 0])]);
		const bars = target.querySelectorAll('[data-testid="facet-bar"]');
		expect(bars[0].querySelector(':scope > div > div')).not.toBeNull();
		expect(bars[1].querySelector(':scope > div > div')).toBeNull();
	});

	it('survives an all-quiet window without dividing by zero', () => {
		const target = render(series, [band('alpha', [0, 0])]);
		expect(target.querySelectorAll('[data-testid="usage-facet"]')).toHaveLength(1);
		expect(target.querySelector('[data-testid="facet-bar"] > div > div')).toBeNull();
	});
});
