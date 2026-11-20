// Dashboard search, filter and sort: pure functions over the server list the page
// already holds, plus the URL query that makes a filtered view linkable.

import { hasActiveStartup } from './startup';
import type { Runner, ServerSummary } from './types';

/** The states a person filters by. `stopping` and an active startup read as
 * `starting` (both are the amber in-progress state on the card). */
export const STATE_FILTERS = ['running', 'idle', 'starting', 'unhealthy', 'failed', 'stopped'] as const;
export type StateFilter = (typeof STATE_FILTERS)[number];

/** The states that need a hand, picked together by the "Problems" chip. */
export const PROBLEM_STATES: readonly StateFilter[] = ['failed', 'unhealthy'];

export const SORTS = ['name', 'state', 'tools', 'added', 'used'] as const;
export type SortKey = (typeof SORTS)[number];

/** `owner`: '' (anyone), 'mine', 'admin' (admin-owned) or a user id. */
export interface ServerQuery {
	q: string;
	states: StateFilter[];
	runners: Runner[];
	owner: string;
	sort: SortKey;
}

export const DEFAULT_QUERY: ServerQuery = { q: '', states: [], runners: [], owner: '', sort: 'name' };

/** The runner badge's label, which search also matches. */
export const RUNNER_LABEL: Record<Runner, string> = {
	npx: 'npx',
	uvx: 'uvx',
	command: 'cmd',
	docker: 'docker',
	remote: 'remote',
	openapi: 'openapi',
	graphql: 'graphql',
	mcpelevator: 'built-in'
};

export const RUNNERS = Object.keys(RUNNER_LABEL) as Runner[];

/** Problems first, then in progress, then the healthy and the parked. */
const STATE_ORDER: Record<StateFilter, number> = {
	failed: 0,
	unhealthy: 1,
	starting: 2,
	running: 3,
	idle: 4,
	stopped: 5
};

export function filterState(server: ServerSummary): StateFilter {
	if (hasActiveStartup(server) || server.state === 'starting' || server.state === 'stopping') {
		return 'starting';
	}
	return server.state;
}

function matchesOwner(server: ServerSummary, owner: string, myId: string | null): boolean {
	const ownerId = server.owner_id ?? null;
	if (!owner) return true;
	if (owner === 'mine') return myId !== null && ownerId === myId;
	if (owner === 'admin') return ownerId === null;
	return ownerId === owner;
}

function matchesText(server: ServerSummary, q: string): boolean {
	const needle = q.trim().toLowerCase();
	if (!needle) return true;
	const haystack = [
		server.name,
		server.slug,
		server.runner,
		RUNNER_LABEL[server.runner] ?? '',
		...(server.tool_names ?? [])
	];
	return haystack.some((s) => s.toLowerCase().includes(needle));
}

/** The servers matching every filter in `query` (sort is ignored). Several states
 * or runners match any of them. `myId` resolves the "mine" owner filter. */
export function filterServers<T extends ServerSummary>(
	servers: T[],
	query: ServerQuery,
	myId: string | null = null
): T[] {
	return servers.filter(
		(s) =>
			matchesText(s, query.q) &&
			(query.states.length === 0 || query.states.includes(filterState(s))) &&
			(query.runners.length === 0 || query.runners.includes(s.runner)) &&
			matchesOwner(s, query.owner, myId)
	);
}

const byName = (a: ServerSummary, b: ServerSummary) =>
	a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true }) ||
	a.slug.localeCompare(b.slug);

/** A sorted copy. `servers` must be in the list route's order (creation order), which
 * is what "added" reverses. `calls` maps a server id to its tool calls for "used";
 * a server missing from it counts as 0. Ties fall back to name. */
export function sortServers<T extends ServerSummary>(
	servers: T[],
	sort: SortKey,
	calls: Record<string, number> = {}
): T[] {
	const indexed = servers.map((server, i) => ({ server, i }));
	const compare: Record<SortKey, (a: (typeof indexed)[0], b: (typeof indexed)[0]) => number> = {
		name: (a, b) => byName(a.server, b.server),
		state: (a, b) =>
			STATE_ORDER[filterState(a.server)] - STATE_ORDER[filterState(b.server)] ||
			byName(a.server, b.server),
		tools: (a, b) => b.server.tools_count - a.server.tools_count || byName(a.server, b.server),
		added: (a, b) => b.i - a.i,
		used: (a, b) =>
			(calls[b.server.id] ?? 0) - (calls[a.server.id] ?? 0) || byName(a.server, b.server)
	};
	return indexed.sort(compare[sort]).map((x) => x.server);
}

/** Does `query` narrow the list at all? (Sort alone doesn't.) */
export function isFiltered(query: ServerQuery): boolean {
	return Boolean(query.q.trim() || query.states.length || query.runners.length || query.owner);
}

function list<T extends string>(raw: string | null, allowed: readonly T[]): T[] {
	const wanted = new Set((raw ?? '').split(','));
	return allowed.filter((v) => wanted.has(v));
}

/** Read a query from the URL. Unknown states, runners and sorts are dropped. */
export function parseQuery(params: Pick<URLSearchParams, 'get'>): ServerQuery {
	const sort = params.get('sort') as SortKey | null;
	return {
		q: params.get('q') ?? '',
		states: list(params.get('state'), STATE_FILTERS),
		runners: list(params.get('runner'), RUNNERS),
		owner: (params.get('owner') ?? '').trim(),
		sort: sort && SORTS.includes(sort) ? sort : 'name'
	};
}

/** The URL search string for `query` ('' or '?…'), leaving defaults out. */
export function serializeQuery(query: ServerQuery): string {
	const params = new URLSearchParams();
	if (query.q) params.set('q', query.q);
	if (query.states.length) params.set('state', query.states.join(','));
	if (query.runners.length) params.set('runner', query.runners.join(','));
	if (query.owner) params.set('owner', query.owner);
	if (query.sort !== 'name') params.set('sort', query.sort);
	const s = params.toString();
	return s ? `?${s}` : '';
}

export interface OwnerOption {
	value: string;
	label: string;
}

/** The owner filter's choices for an admin: mine (when the admin is a real user),
 * admin-owned, then each other user who owns a server in the list. */
export function ownerOptions(servers: ServerSummary[], myId: string | null): OwnerOption[] {
	const options: OwnerOption[] = [];
	if (myId) options.push({ value: 'mine', label: 'Mine' });
	options.push({ value: 'admin', label: 'Admin-owned' });
	const users = new Map<string, string>();
	for (const s of servers) {
		if (s.owner_id && s.owner_id !== myId) users.set(s.owner_id, s.owner_name ?? s.owner_id);
	}
	[...users]
		.sort((a, b) => a[1].localeCompare(b[1]))
		.forEach(([value, label]) => options.push({ value, label }));
	return options;
}
