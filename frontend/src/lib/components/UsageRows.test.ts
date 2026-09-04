import { mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { UsageRow } from '$lib/usage';
import UsageRows from './UsageRows.svelte';

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function render(rows: UsageRow[], props: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(UsageRows, { target: host, props: { rows, ...props } });
	return host;
}

function row(overrides: Partial<UsageRow> = {}): UsageRow {
	return {
		key: overrides.label ?? 'k',
		label: 'tool',
		sublabel: null,
		calls: 0,
		other: 0,
		lastCall: null,
		href: null,
		badge: null,
		meta: null,
		...overrides
	};
}

afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	component = null;
	host = null;
});

describe('UsageRows', () => {
	it('renders a table row per entry, never-called included', () => {
		const target = render([
			row({ key: 'a', label: 'used', calls: 4, lastCall: '2026-09-02T10:00:00Z' }),
			row({ key: 'b', label: 'never_used', calls: 0 })
		]);
		const rows = target.querySelectorAll('[data-testid="usage-row"]');
		expect(rows).toHaveLength(2);
		expect([...rows].map((r) => r.getAttribute('data-calls'))).toEqual(['4', '0']);
		// A never-called row reads "never" rather than an empty cell.
		expect(rows[1].textContent).toContain('never');
	});

	it('links a row to its server when it has a destination', () => {
		const target = render([row({ key: 'a', label: 'echo', href: '/server/srv1' })]);
		expect(target.querySelector('a')?.getAttribute('href')).toBe('/server/srv1');
	});

	it('shows the badge and meta qualifiers', () => {
		const target = render([
			row({ key: 'a', label: 'gone', badge: 'retired', meta: '1/4 tools used' })
		]);
		const text = target.textContent ?? '';
		expect(text).toContain('retired');
		expect(text).toContain('1/4 tools used');
	});

	it('keeps the meta qualifier in bar style too', () => {
		// A server reached only by initialize/tools/list survives "Used only" and the
		// summary counts it active, but its bar is empty and its count is 0. Without
		// the meta line this style renders it identically to a wholly untouched
		// server — the state the breakdown exists to distinguish.
		const target = render(
			[row({ key: 'a', label: 'quiet', calls: 0, other: 4, meta: '4 other requests' })],
			{ style: 'bars' }
		);
		expect(target.querySelector('[data-testid="usage-rows-bars"]')).not.toBeNull();
		expect(target.textContent ?? '').toContain('4 other requests');
	});

	it('scales bars against the busiest row in bar style', () => {
		const target = render(
			[row({ key: 'a', label: 'a', calls: 10 }), row({ key: 'b', label: 'b', calls: 5 })],
			{ style: 'bars' }
		);
		const bars = [...target.querySelectorAll('[data-testid="usage-row"]')].map(
			(entry) => (entry.querySelector('div > div') as HTMLElement).style.width
		);
		expect(bars).toEqual(['100%', '50%']);
	});

	it('renders the empty message instead of an empty table', () => {
		const target = render([], { emptyMessage: 'Nothing matches this filter.' });
		expect(target.textContent).toContain('Nothing matches this filter.');
		expect(target.querySelector('[data-testid="usage-row"]')).toBeNull();
	});

	it('labels the identity column for the listing it shows', () => {
		const target = render([row({ key: 'a', label: 'srv' })], { labelHeading: 'Server' });
		expect(target.querySelector('th')?.textContent?.trim()).toBe('Server');
	});
});
