import { describe, expect, it } from 'vitest';

import type { InstanceUsage } from './types';
import {
	DEFAULT_VIEW,
	HEATMAP_DAYS,
	applyView,
	countLabel,
	facetPeak,
	heatLevel,
	heatmap,
	OVERFLOW_TOOL,
	peakCalls,
	serverRows,
	tickLabel,
	toChartPoints,
	toFacetPoints,
	toolBadge,
	toolRows,
	type UsageRow
} from './usage';

function usage(overrides: Partial<InstanceUsage> = {}): InstanceUsage {
	return {
		since: '2026-09-01T00:00:00Z',
		bucket_seconds: 86400,
		tool_calls: 0,
		other_requests: 0,
		last_call_at: null,
		active_servers: 0,
		servers: [],
		tools: [],
		series: [],
		series_by_server: [],
		hourly: [],
		...overrides
	};
}

function row(overrides: Partial<UsageRow> = {}): UsageRow {
	return {
		key: overrides.label ?? 'k',
		label: 'a',
		sublabel: null,
		calls: 0,
		lastCall: null,
		href: null,
		badge: null,
		meta: null,
		...overrides
	};
}

describe('serverRows', () => {
	it('keeps a server nothing has touched, at zero', () => {
		// Finding the unused server is half of what the dashboard is for.
		const [only] = serverRows(
			usage({
				servers: [
					{
						server_id: 'srv1',
						slug: 'quiet',
						name: 'Quiet',
						tool_calls: 0,
						other_requests: 0,
						last_call_at: null,
						tools_called: 0,
						tools_known: 4
					}
				]
			})
		);
		expect(only).toMatchObject({ label: 'Quiet', sublabel: 'quiet', calls: 0, lastCall: null });
		expect(only.meta).toBe('0/4 tools used');
		expect(only.href).toBe('/server/srv1');
	});

	it('flags a server that is exposing no tools right now', () => {
		const [only] = serverRows(
			usage({
				servers: [
					{
						server_id: 'srv1',
						slug: 's',
						name: 'S',
						tool_calls: 2,
						other_requests: 0,
						last_call_at: '2026-09-02T10:00:00Z',
						tools_called: 1,
						tools_known: 0
					}
				]
			})
		);
		expect(only.badge).toBe('no tools listed');
		expect(only.meta).toBeNull();
	});
});

describe('toolRows', () => {
	it('marks a tool the server no longer exposes', () => {
		const rows = toolRows(
			usage({
				tools: [
					{
						server_id: 'srv1',
						slug: 'files',
						tool: 'gone',
						calls: 3,
						last_call_at: '2026-09-02T10:00:00Z',
						known: false
					},
					{
						server_id: 'srv1',
						slug: 'files',
						tool: 'kept',
						calls: 0,
						last_call_at: null,
						known: true
					}
				]
			})
		);
		expect(rows.map((r) => [r.label, r.badge])).toEqual([
			['gone', 'retired'],
			['kept', null]
		]);
		// Keys carry the server too: two servers may expose the same tool name.
		expect(rows[0].key).toBe('srv1:gone');
	});

	it('does not call the overflow pool a retired tool', () => {
		// "retired" means the server used to expose this name — a prompt to look at
		// a rename. The pool is unrecognised traffic that never was a tool, so
		// labelling it retired would report a tool that never existed AND hide the
		// one condition the row exists to communicate.
		const rows = toolRows(
			usage({
				tools: [
					{
						server_id: 'srv1',
						slug: 'files',
						tool: OVERFLOW_TOOL,
						calls: 9,
						last_call_at: '2026-09-02T10:00:00Z',
						known: false
					}
				]
			})
		);
		expect(rows[0].badge).toBe('unrecognised');
	});
});

describe('toolBadge', () => {
	it('separates the three kinds of row', () => {
		expect(toolBadge('search', true)).toBeNull();
		expect(toolBadge('search', false)).toBe('retired');
		expect(toolBadge(OVERFLOW_TOOL, false)).toBe('unrecognised');
	});
});

describe('applyView', () => {
	const rows = [
		row({ key: 'a', label: 'alpha', calls: 5, lastCall: '2026-09-01T00:00:00Z' }),
		row({ key: 'b', label: 'bravo', calls: 0, lastCall: null }),
		row({ key: 'c', label: 'charlie', calls: 2, lastCall: '2026-09-03T00:00:00Z' })
	];

	it('sorts by most calls by default', () => {
		expect(applyView(rows, DEFAULT_VIEW).map((r) => r.label)).toEqual([
			'alpha',
			'charlie',
			'bravo'
		]);
	});

	it('sorts least-used first — the tool worth renaming', () => {
		expect(applyView(rows, { ...DEFAULT_VIEW, sort: 'calls-asc' }).map((r) => r.label)).toEqual([
			'bravo',
			'charlie',
			'alpha'
		]);
	});

	it('sorts by recency with never-called last', () => {
		expect(applyView(rows, { ...DEFAULT_VIEW, sort: 'recent' }).map((r) => r.label)).toEqual([
			'charlie',
			'alpha',
			'bravo'
		]);
	});

	it('sorts by name', () => {
		expect(applyView(rows, { ...DEFAULT_VIEW, sort: 'name' }).map((r) => r.label)).toEqual([
			'alpha',
			'bravo',
			'charlie'
		]);
	});

	it('filters case-insensitively on label and sublabel', () => {
		const withSub = [...rows, row({ key: 'd', label: 'delta', sublabel: 'ALPHA-server' })];
		expect(applyView(withSub, { ...DEFAULT_VIEW, search: 'ALPHA' }).map((r) => r.label)).toEqual([
			'alpha',
			'delta'
		]);
	});

	it('hides never-called rows only when asked', () => {
		expect(applyView(rows, { ...DEFAULT_VIEW, hideUnused: true }).map((r) => r.label)).toEqual([
			'alpha',
			'charlie'
		]);
	});

	it('does not mutate the input listing', () => {
		const original = rows.map((r) => r.label);
		applyView(rows, { ...DEFAULT_VIEW, sort: 'name' });
		expect(rows.map((r) => r.label)).toEqual(original);
	});
});

describe('peakCalls', () => {
	it('never returns zero, so an all-quiet listing still divides', () => {
		expect(peakCalls([row({ calls: 0 }), row({ calls: 0 })])).toBe(1);
		expect(peakCalls([])).toBe(1);
		expect(peakCalls([row({ calls: 3 }), row({ calls: 9 })])).toBe(9);
	});
});

describe('chart data', () => {
	it('turns the API’s ISO buckets into Dates for the time axis', () => {
		const [first] = toChartPoints([{ bucket: '2026-09-01T10:00:00Z', calls: 3, other: 1 }]);
		expect(first.bucket).toBeInstanceOf(Date);
		expect(first.bucket.toISOString()).toBe('2026-09-01T10:00:00.000Z');
		expect(first).toMatchObject({ calls: 3, other: 1 });
	});

	it('positions a band by the series it is aligned to', () => {
		const series = [
			{ bucket: '2026-09-01T10:00:00Z', calls: 0, other: 0 },
			{ bucket: '2026-09-01T11:00:00Z', calls: 0, other: 0 }
		];
		const points = toFacetPoints(
			{ server_id: 'a', slug: 'a', name: 'A', points: [4, 2] },
			series
		);
		expect(points.map((p) => [p.bucket.toISOString(), p.calls])).toEqual([
			['2026-09-01T10:00:00.000Z', 4],
			['2026-09-01T11:00:00.000Z', 2]
		]);
	});

	it('truncates a band longer than the series rather than inventing times', () => {
		const series = [{ bucket: '2026-09-01T10:00:00Z', calls: 0, other: 0 }];
		const points = toFacetPoints(
			{ server_id: 'a', slug: 'a', name: 'A', points: [1, 2, 3] },
			series
		);
		expect(points).toHaveLength(1);
	});

	it('shares one y domain across facets, and never a zero one', () => {
		expect(
			facetPeak([
				{ server_id: 'a', slug: 'a', name: 'A', points: [4, 0] },
				{ server_id: 'b', slug: 'b', name: 'B', points: [9, 1] }
			])
		).toBe(9);
		expect(facetPeak([{ server_id: 'a', slug: 'a', name: 'A', points: [0, 0] }])).toBe(1);
		expect(facetPeak([])).toBe(1);
	});

	it('labels ticks as clock times or dates by bucket width', () => {
		const at = '2026-09-01T10:00:00Z';
		expect(tickLabel(at, true)).toBe(
			new Date(at).toLocaleTimeString(undefined, { hour: 'numeric' })
		);
		expect(tickLabel(at, false)).toBe(
			new Date(at).toLocaleDateString(undefined, {
				month: 'short',
				day: 'numeric',
				timeZone: 'UTC'
			})
		);
		expect(tickLabel('not-a-date', true)).toBe('not-a-date');
	});

	it('names a daily bucket by its UTC day, not the reader’s', () => {
		// A daily bucket is anchored at UTC midnight and holds that whole UTC day.
		// Rendered locally, every reader west of UTC would see the day before while
		// looking at the following day's counts.
		//
		// The suite runs under TZ=America/Los_Angeles (see package.json) precisely so
		// this guard bites: that instant is Aug 31 locally and Sep 1 in UTC, so the
		// assertion below fails the moment `tickLabel` loses `timeZone: 'UTC'`. A
		// UTC runner would make the two indistinguishable and the test vacuous.
		const utcMidnight = '2026-09-01T00:00:00Z';
		const local = new Date(utcMidnight).getDate();
		const inUtc = Number(
			new Intl.DateTimeFormat('en-US', { day: 'numeric', timeZone: 'UTC' }).format(
				new Date(utcMidnight)
			)
		);
		expect(local, 'the suite must not run in UTC or this test proves nothing').not.toBe(
			inUtc
		);
		expect(tickLabel(utcMidnight, false)).toBe(
			new Date(utcMidnight).toLocaleDateString(undefined, {
				month: 'short',
				day: 'numeric',
				timeZone: 'UTC'
			})
		);
	});
});

describe('heatmap', () => {
	it('buckets UTC hours into the reader’s local weekday and hour', () => {
		// Asserted against the same Date the component would use, so the test states
		// the contract (local bucketing) without pinning the runner's timezone.
		const iso = '2026-09-02T14:00:00Z';
		const local = new Date(iso);
		const grid = heatmap([{ bucket: iso, calls: 3 }]);
		const row = HEATMAP_DAYS.indexOf(local.getDay());
		expect(grid.cells[row][local.getHours()]).toBe(3);
		expect(grid.total).toBe(3);
		expect(grid.peak).toBe(3);
	});

	it('sums repeat visits to the same weekday hour across the window', () => {
		const a = '2026-09-02T14:00:00Z';
		const b = '2026-09-09T14:00:00Z'; // same weekday + hour, a week later
		const grid = heatmap([
			{ bucket: a, calls: 2 },
			{ bucket: b, calls: 5 }
		]);
		const local = new Date(a);
		expect(grid.cells[HEATMAP_DAYS.indexOf(local.getDay())][local.getHours()]).toBe(7);
		expect(grid.peak).toBe(7);
	});

	it('is a full 7x24 grid even with no data, and ignores junk timestamps', () => {
		const grid = heatmap([{ bucket: 'not-a-date', calls: 9 }]);
		expect(grid.cells).toHaveLength(7);
		expect(grid.cells.every((row) => row.length === 24)).toBe(true);
		expect(grid.total).toBe(0);
		expect(grid.peak).toBe(0);
	});
});

describe('heatLevel', () => {
	it('keeps empty distinct from the lightest data step', () => {
		expect(heatLevel(0, 10)).toBe(0);
		expect(heatLevel(1, 10)).toBe(1);
	});

	it('steps up to the busiest cell', () => {
		expect(heatLevel(10, 10)).toBe(4);
		expect(heatLevel(5, 10)).toBe(2);
		expect(heatLevel(6, 10)).toBe(3);
	});

	it('never divides by a zero peak', () => {
		expect(heatLevel(3, 0)).toBe(1);
	});
});

describe('countLabel', () => {
	it('says how much of the listing a filter is showing', () => {
		expect(countLabel(3, 3, 'tool')).toBe('3 tools');
		expect(countLabel(2, 7, 'tool')).toBe('2 of 7 tools');
		expect(countLabel(1, 1, 'server')).toBe('1 server');
	});
});
