// Usage dashboard view model: normalize, filter, sort.
//
// Servers and tools are two different shapes on the wire but the SAME question
// on screen ("what is used, what isn't, when was it last touched"), so both are
// normalized to one `UsageRow` here and rendered by one set of components. The
// listings arrive whole from `GET /api/usage` — bounded by servers x tools — so
// filtering and sorting happen in the browser and answer instantly.
//
// Pure functions, no Svelte: the page wires them to inputs, and the rules are
// unit-testable on their own.

import type { InstanceUsage, UsageHour } from './types';

// NOTE ON COLOUR. This app's design system declares one locked accent (see
// app.css) and supplies no categorical palette — so no view here invents one. A
// split-by-server view is rendered as SMALL MULTIPLES instead of a stacked
// multi-hue chart: one sparkline per server, each in the accent, identity carried
// by its own title rather than by a hue the system doesn't have. That is the
// documented way out when you run out of categorical slots, and here the system
// has none to begin with.

/** Weekday rows of the activity grid, Monday first — a work-week reads better
 * than a calendar week for "when is this thing used". */
export const HEATMAP_DAYS = [1, 2, 3, 4, 5, 6, 0];

export interface Heatmap {
	/** cells[row][hour] — rows follow HEATMAP_DAYS, hours are 0–23 LOCAL. */
	cells: number[][];
	peak: number;
	total: number;
}

/**
 * Fold sparse hourly counts into a weekday x hour grid in the READER's timezone.
 * The API reports UTC hours precisely because only the browser knows which local
 * hour they belong to — bucketing server-side would label a European operator's
 * morning as someone else's night.
 */
export function heatmap(hourly: UsageHour[]): Heatmap {
	const cells = HEATMAP_DAYS.map(() => new Array<number>(24).fill(0));
	const rowOf = new Map(HEATMAP_DAYS.map((day, index) => [day, index]));
	let peak = 0;
	let total = 0;
	for (const hour of hourly) {
		const at = new Date(hour.bucket);
		if (Number.isNaN(at.getTime())) continue;
		const row = rowOf.get(at.getDay());
		if (row === undefined) continue;
		const value = (cells[row][at.getHours()] += hour.calls);
		if (value > peak) peak = value;
		total += hour.calls;
	}
	return { cells, peak, total };
}

/** Which step of the sequential ramp a cell sits on: 0 (empty) then 1–4. A
 * stepped ramp reads as a legend-able scale where a continuous one reads as
 * noise. */
export function heatLevel(value: number, peak: number): number {
	if (value <= 0) return 0;
	if (peak <= 0) return 1;
	return Math.min(4, Math.ceil((value / peak) * 4));
}

/** One row of a breakdown listing — a server or a tool, rendered identically. */
export interface UsageRow {
	/** Stable key for `{#each}` and for the row's identity across re-sorts. */
	key: string;
	label: string;
	/** Secondary identifier (a tool's server), shown next to the label. */
	sublabel: string | null;
	calls: number;
	/** ISO timestamp of the last call, or null when nothing ever called it. */
	lastCall: string | null;
	/** Where clicking the row goes, when it has a destination. */
	href: string | null;
	/** Short qualifier: a tool the server no longer exposes, a server that is
	 * exposing nothing right now. */
	badge: string | null;
	/** Extra context for the row (e.g. how many of a server's tools got used). */
	meta: string | null;
}

export type UsageSort = 'calls-desc' | 'calls-asc' | 'recent' | 'name';

/** Sort options in the order the picker offers them. `calls-asc` is not a
 * curiosity: "least used first" is how you find the tool worth renaming, which
 * is the question the whole panel exists for. */
export const USAGE_SORTS: { value: UsageSort; label: string }[] = [
	{ value: 'calls-desc', label: 'Most calls' },
	{ value: 'calls-asc', label: 'Least calls' },
	{ value: 'recent', label: 'Recently used' },
	{ value: 'name', label: 'Name' }
];

export interface UsageView {
	search: string;
	sort: UsageSort;
	/** Drop rows with no calls at all in the window. */
	hideUnused: boolean;
}

export const DEFAULT_VIEW: UsageView = {
	search: '',
	sort: 'calls-desc',
	hideUnused: false
};

/** Server rows: every visible server, including the ones nothing touched. */
export function serverRows(usage: InstanceUsage): UsageRow[] {
	return usage.servers.map((server) => ({
		key: server.server_id,
		label: server.name,
		sublabel: server.slug,
		calls: server.tool_calls,
		lastCall: server.last_call_at,
		href: `/server/${encodeURIComponent(server.server_id)}`,
		// tools_known is 0 while a server isn't running (nothing discovered), which
		// is worth saying out loud rather than rendering a bare "0 of 0".
		badge: server.tools_known === 0 ? 'no tools listed' : null,
		meta:
			server.tools_known > 0
				? `${server.tools_called}/${server.tools_known} tools used`
				: null
	}));
}

/** Tool rows across every visible server, including never-called ones. */
export function toolRows(usage: InstanceUsage): UsageRow[] {
	return usage.tools.map((tool) => ({
		key: `${tool.server_id}:${tool.tool}`,
		label: tool.tool,
		sublabel: tool.slug,
		calls: tool.calls,
		lastCall: tool.last_call_at,
		href: `/server/${encodeURIComponent(tool.server_id)}`,
		badge: tool.known ? null : 'retired',
		meta: null
	}));
}

function matches(row: UsageRow, needle: string): boolean {
	return (
		row.label.toLowerCase().includes(needle) ||
		(row.sublabel?.toLowerCase().includes(needle) ?? false)
	);
}

function byName(a: UsageRow, b: UsageRow): number {
	return a.label.localeCompare(b.label) || (a.sublabel ?? '').localeCompare(b.sublabel ?? '');
}

function lastCallValue(row: UsageRow): number {
	if (!row.lastCall) return -Infinity; // never called sorts last under "recent"
	const at = Date.parse(row.lastCall);
	return Number.isNaN(at) ? -Infinity : at;
}

const COMPARATORS: Record<UsageSort, (a: UsageRow, b: UsageRow) => number> = {
	'calls-desc': (a, b) => b.calls - a.calls || byName(a, b),
	'calls-asc': (a, b) => a.calls - b.calls || byName(a, b),
	recent: (a, b) => lastCallValue(b) - lastCallValue(a) || byName(a, b),
	name: byName
};

/** Apply the toolbar to a listing. Never mutates the input — the page keeps the
 * unfiltered rows so clearing a filter costs no refetch. */
export function applyView(rows: UsageRow[], view: UsageView): UsageRow[] {
	const needle = view.search.trim().toLowerCase();
	const filtered = rows.filter(
		(row) => (!view.hideUnused || row.calls > 0) && (!needle || matches(row, needle))
	);
	return [...filtered].sort(COMPARATORS[view.sort]);
}

/** The busiest row's call count, for scaling proportional bars. Never 0, so a
 * listing where nothing has been called still divides. */
export function peakCalls(rows: UsageRow[]): number {
	return Math.max(1, ...rows.map((row) => row.calls));
}

/** "3 of 12 servers" style summary of what a filter is currently showing. */
export function countLabel(shown: number, total: number, noun: string): string {
	const plural = total === 1 ? noun : `${noun}s`;
	return shown === total ? `${total} ${plural}` : `${shown} of ${total} ${plural}`;
}
