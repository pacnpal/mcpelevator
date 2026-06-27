import { describe, expect, it } from 'vitest';

import { installOptions, INSTALL_GROUP_ORDER, type InstallOption } from './install';
import type { ServerSummary } from './types';

type Args = Pick<ServerSummary, 'slug' | 'urls' | 'auth'>;

const base: Args = {
	slug: 'memory',
	urls: { mcp: 'http://127.0.0.1:8080/s/memory/mcp', rest: null },
	auth: 'none'
};

function byLabel(opts: InstallOption[], label: string): InstallOption {
	const o = opts.find((x) => x.label === label);
	if (!o) throw new Error(`no option labelled ${label}`);
	return o;
}

describe('installOptions', () => {
	it('emits a snippet for every supported client', () => {
		const labels = installOptions(base).map((o) => o.label);
		expect(labels).toEqual([
			'Claude Code',
			'Claude Desktop',
			'Claude web / mobile',
			'Codex',
			'ChatGPT',
			'Gemini CLI',
			'VS Code',
			'mcpServers',
			'MCP URL'
		]);
	});

	it('returns nothing when there are no URLs', () => {
		expect(installOptions({ ...base, urls: { mcp: null, rest: null } })).toEqual([]);
	});

	it('only emits the REST URL when there is no MCP URL', () => {
		const opts = installOptions({
			...base,
			urls: { mcp: null, rest: 'http://127.0.0.1:8080/s/memory/rest' }
		});
		expect(opts.map((o) => o.label)).toEqual(['REST URL']);
	});

	it('assigns every option to a known group', () => {
		for (const opt of installOptions({ ...base, urls: { ...base.urls, rest: 'http://x/rest' } })) {
			expect(INSTALL_GROUP_ORDER).toContain(opt.group);
		}
	});

	it('Claude Desktop bridges remote HTTP through mcp-remote (stdio)', () => {
		const entry = JSON.parse(byLabel(installOptions(base), 'Claude Desktop').value);
		expect(entry.mcpServers.memory).toEqual({
			command: 'npx',
			args: ['mcp-remote', base.urls.mcp]
		});
	});

	it('Gemini CLI uses httpUrl for Streamable HTTP', () => {
		const entry = JSON.parse(byLabel(installOptions(base), 'Gemini CLI').value);
		expect(entry.mcpServers.memory).toEqual({ httpUrl: base.urls.mcp });
	});

	it('mcpServers/VS Code use type:"http" remote entries', () => {
		const opts = installOptions(base);
		expect(JSON.parse(byLabel(opts, 'mcpServers').value).mcpServers.memory).toEqual({
			type: 'http',
			url: base.urls.mcp
		});
		expect(JSON.parse(byLabel(opts, 'VS Code').value).servers.memory).toEqual({
			type: 'http',
			url: base.urls.mcp
		});
	});

	it('URL-only connectors (Claude web, ChatGPT) carry the raw MCP URL', () => {
		const opts = installOptions(base);
		expect(byLabel(opts, 'Claude web / mobile').value).toBe(base.urls.mcp);
		expect(byLabel(opts, 'ChatGPT').value).toBe(base.urls.mcp);
	});

	describe('bearer auth', () => {
		const bearer: Args = { ...base, auth: 'bearer' };

		it('adds an Authorization header to the Claude Code command', () => {
			expect(byLabel(installOptions(bearer), 'Claude Code').value).toContain(
				'--header "Authorization: Bearer <YOUR_TOKEN>"'
			);
		});

		it('threads the token header through mcp-remote for Claude Desktop', () => {
			const entry = JSON.parse(byLabel(installOptions(bearer), 'Claude Desktop').value);
			expect(entry.mcpServers.memory.args).toEqual([
				'mcp-remote',
				base.urls.mcp,
				'--header',
				'Authorization: Bearer <YOUR_TOKEN>'
			]);
		});

		it('adds headers to the Gemini CLI and mcpServers entries', () => {
			const opts = installOptions(bearer);
			expect(JSON.parse(byLabel(opts, 'Gemini CLI').value).mcpServers.memory.headers).toEqual({
				Authorization: 'Bearer <YOUR_TOKEN>'
			});
			expect(JSON.parse(byLabel(opts, 'mcpServers').value).mcpServers.memory.headers).toEqual({
				Authorization: 'Bearer <YOUR_TOKEN>'
			});
		});
	});
});
