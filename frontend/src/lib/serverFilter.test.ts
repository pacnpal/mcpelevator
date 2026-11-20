import { describe, expect, it } from 'vitest';

import {
	DEFAULT_QUERY,
	filterServers,
	isFiltered,
	ownerOptions,
	parseQuery,
	serializeQuery,
	sortServers,
	type ServerQuery
} from './serverFilter';
import type { Runner, ServerState, ServerSummary } from './types';

function server(
	name: string,
	over: Partial<ServerSummary> & { state?: ServerState; runner?: Runner } = {}
): ServerSummary {
	return {
		id: name,
		slug: name.toLowerCase().replace(/\s+/g, '-'),
		name,
		runner: 'npx',
		enabled: true,
		state: 'running',
		startup_status: null,
		transports: { mcp_http: true, rest_openapi: false },
		urls: { mcp: null, rest: null },
		auth: 'none',
		last_error: null,
		pid: null,
		port: null,
		tools_count: 0,
		tool_names: [],
		owner_id: null,
		owner_name: null,
		...over
	} as ServerSummary;
}

const q = (over: Partial<ServerQuery>): ServerQuery => ({ ...DEFAULT_QUERY, ...over });
const names = (list: ServerSummary[]) => list.map((s) => s.name);

describe('filterServers', () => {
	const servers = [
		server('Files', { tool_names: ['read_file', 'write_file'] }),
		server('GitHub', { runner: 'docker', state: 'failed', tool_names: ['create_issue'] }),
		server('Weather', { runner: 'remote', state: 'unhealthy' }),
		server('Petstore', { runner: 'openapi', state: 'idle' }),
		server('Control', { runner: 'mcpelevator', state: 'stopped', enabled: false })
	];

	it('searches name, slug, runner and tool names, ignoring case', () => {
		expect(names(filterServers(servers, q({ q: 'WRITE_FI' })))).toEqual(['Files']);
		expect(names(filterServers(servers, q({ q: 'github' })))).toEqual(['GitHub']);
		expect(names(filterServers(servers, q({ q: 'openapi' })))).toEqual(['Petstore']);
		expect(names(filterServers(servers, q({ q: 'built-in' })))).toEqual(['Control']);
		expect(filterServers(servers, q({ q: 'nothing-like-this' }))).toEqual([]);
	});

	it('finds a server by a tool name the list route sends', () => {
		// The shape GET /api/servers returns: exposed names, a renamed tool under its new name.
		const listed = [server('Docs', { tool_names: ['search_docs', 'save_file'] }), server('Other')];
		expect(names(filterServers(listed, q({ q: 'Save_File' })))).toEqual(['Docs']);
		expect(names(filterServers(listed, q({ q: 'write_file' })))).toEqual([]);
	});

	it('ORs several states together', () => {
		expect(names(filterServers(servers, q({ states: ['failed', 'unhealthy'] })))).toEqual([
			'GitHub',
			'Weather'
		]);
	});

	it('counts an active startup and stopping as starting', () => {
		const list = [
			server('Booting', {
				state: 'failed',
				startup_status: { phase: 'setup' } as ServerSummary['startup_status']
			}),
			server('Leaving', { state: 'stopping' }),
			server('Up')
		];
		expect(names(filterServers(list, q({ states: ['starting'] })))).toEqual(['Booting', 'Leaving']);
	});

	it('filters by runner, ORing several', () => {
		expect(names(filterServers(servers, q({ runners: ['docker', 'remote'] })))).toEqual([
			'GitHub',
			'Weather'
		]);
	});

	it('filters by owner: mine, admin-owned and one user', () => {
		const list = [
			server('A', { owner_id: 'me' }),
			server('B'),
			server('C', { owner_id: 'u2', owner_name: 'sam' })
		];
		expect(names(filterServers(list, q({ owner: 'mine' }), 'me'))).toEqual(['A']);
		expect(names(filterServers(list, q({ owner: 'mine' }), null))).toEqual([]);
		expect(names(filterServers(list, q({ owner: 'admin' }), 'me'))).toEqual(['B']);
		expect(names(filterServers(list, q({ owner: 'u2' }), 'me'))).toEqual(['C']);
	});

	it('combines filters with AND', () => {
		expect(
			names(filterServers(servers, q({ states: ['failed', 'idle'], runners: ['openapi'] })))
		).toEqual(['Petstore']);
	});
});

describe('sortServers', () => {
	// List-route order is creation order.
	const servers = [
		server('beta', { state: 'stopped', tools_count: 2 }),
		server('Alpha', { state: 'idle', tools_count: 9 }),
		server('gamma', { state: 'running', tools_count: 2 }),
		server('delta', { state: 'starting' }),
		server('epsilon', { state: 'unhealthy' }),
		server('zeta', { state: 'failed' })
	];

	it('sorts by name, ignoring case', () => {
		expect(names(sortServers(servers, 'name'))).toEqual([
			'Alpha',
			'beta',
			'delta',
			'epsilon',
			'gamma',
			'zeta'
		]);
	});

	it('sorts problems first, then starting, running, idle, stopped', () => {
		expect(names(sortServers(servers, 'state'))).toEqual([
			'zeta',
			'epsilon',
			'delta',
			'gamma',
			'Alpha',
			'beta'
		]);
	});

	it('sorts by tool count, most first, ties by name', () => {
		expect(names(sortServers(servers, 'tools')).slice(0, 3)).toEqual(['Alpha', 'beta', 'gamma']);
	});

	it('sorts recently added first', () => {
		expect(names(sortServers(servers, 'added'))).toEqual([
			'zeta',
			'epsilon',
			'delta',
			'gamma',
			'Alpha',
			'beta'
		]);
	});

	it('sorts most used first, missing usage as zero', () => {
		expect(names(sortServers(servers, 'used', { gamma: 40, beta: 3 })).slice(0, 3)).toEqual([
			'gamma',
			'beta',
			'Alpha'
		]);
	});

	it('leaves the input untouched', () => {
		const copy = [...servers];
		sortServers(servers, 'state');
		expect(servers).toEqual(copy);
	});
});

describe('URL query', () => {
	it('round-trips a full query', () => {
		const query: ServerQuery = {
			q: 'git hub',
			states: ['unhealthy', 'failed'],
			runners: ['docker', 'mcpelevator'],
			owner: 'u2',
			sort: 'state'
		};
		const back = parseQuery(new URLSearchParams(serializeQuery(query)));
		expect(back).toEqual(query);
		expect(serializeQuery(back)).toBe(serializeQuery(query));
	});

	it('leaves defaults out of the URL', () => {
		expect(serializeQuery(DEFAULT_QUERY)).toBe('');
		expect(parseQuery(new URLSearchParams(''))).toEqual(DEFAULT_QUERY);
	});

	it('drops unknown states, runners and sorts', () => {
		const parsed = parseQuery(
			new URLSearchParams('state=failed,bogus,,RUNNING&runner=npx,python&sort=random')
		);
		expect(parsed.states).toEqual(['failed']);
		expect(parsed.runners).toEqual(['npx']);
		expect(parsed.sort).toBe('name');
	});

	it('treats a sort or empty lists as unfiltered', () => {
		expect(isFiltered(q({ sort: 'tools' }))).toBe(false);
		expect(isFiltered(q({ q: '  ' }))).toBe(false);
		expect(isFiltered(q({ runners: ['uvx'] }))).toBe(true);
	});
});

describe('ownerOptions', () => {
	it('offers mine, admin-owned, then each other owner by name', () => {
		const list = [
			server('A', { owner_id: 'u3', owner_name: 'zoe' }),
			server('B', { owner_id: 'me', owner_name: 'me' }),
			server('C', { owner_id: 'u2', owner_name: 'ann' }),
			server('D', { owner_id: 'u2', owner_name: 'ann' })
		];
		expect(ownerOptions(list, 'me').map((o) => o.value)).toEqual(['mine', 'admin', 'u2', 'u3']);
		expect(ownerOptions(list, null)[0].value).toBe('admin');
	});
});
