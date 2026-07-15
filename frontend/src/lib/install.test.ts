import { describe, expect, it } from 'vitest';

import { installOptions, INSTALL_GROUP_ORDER, type InstallOption } from './install';
import type { ServerSummary } from './types';

type Args = Pick<ServerSummary, 'slug' | 'urls' | 'auth'>;

// Default local install: http loopback URL (not publicly reachable).
const base: Args = {
	slug: 'memory',
	urls: { mcp: 'http://127.0.0.1:8080/s/memory/mcp', rest: null },
	auth: 'none'
};

// Exposed install behind a public HTTPS base URL.
const PUBLIC_MCP = 'https://mcp.example.com/s/memory/mcp';
const pub: Args = { ...base, urls: { mcp: PUBLIC_MCP, rest: null } };

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

	it('does not surface a REST-only server (REST/OpenAPI not served yet)', () => {
		const opts = installOptions({
			...base,
			urls: { mcp: null, rest: 'http://127.0.0.1:8080/s/memory/rest' }
		});
		expect(opts).toEqual([]);
	});

	it('assigns every option to a known group', () => {
		for (const opt of installOptions({ ...base, urls: { ...base.urls, rest: 'http://x/rest' } })) {
			expect(INSTALL_GROUP_ORDER).toContain(opt.group);
		}
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

	describe('Claude Desktop (mcp-remote bridge)', () => {
		it('bridges remote HTTP through mcp-remote (stdio), auto-accepting the npx prompt', () => {
			const entry = JSON.parse(byLabel(installOptions(pub), 'Claude Desktop').value);
			expect(entry.mcpServers.memory).toEqual({
				command: 'npx',
				args: ['-y', 'mcp-remote', PUBLIC_MCP]
			});
		});

		it('adds --allow-http for non-https (local) endpoints', () => {
			const entry = JSON.parse(byLabel(installOptions(base), 'Claude Desktop').value);
			expect(entry.mcpServers.memory.args).toEqual([
				'-y',
				'mcp-remote',
				base.urls.mcp,
				'--allow-http'
			]);
		});
	});

	describe('reachability caveat for cloud connectors', () => {
		it('warns that a local URL needs a public HTTPS URL', () => {
			const opts = installOptions(base);
			expect(byLabel(opts, 'Claude web / mobile').hint).toContain('needs a public HTTPS URL');
			expect(byLabel(opts, 'ChatGPT').hint).toContain('needs a public HTTPS URL');
		});

		it('omits the reachability caveat for a public HTTPS URL', () => {
			const opts = installOptions(pub);
			expect(byLabel(opts, 'Claude web / mobile').hint).not.toContain('needs a public');
			expect(byLabel(opts, 'ChatGPT').hint).not.toContain('needs a public');
		});

		it('warns for private HTTPS hosts a vendor cloud cannot reach', () => {
			const privateHosts = [
				'https://10.0.0.5/s/memory/mcp',
				'https://192.168.1.10/s/memory/mcp',
				'https://172.16.4.4/s/memory/mcp',
				'https://100.100.100.100/s/memory/mcp', // CGNAT/Tailscale
				'https://localhost:8443/s/memory/mcp',
				'https://mcp.lab.local/s/memory/mcp'
			];
			for (const mcp of privateHosts) {
				const opts = installOptions({ ...base, urls: { mcp, rest: null } });
				expect(byLabel(opts, 'ChatGPT').hint).toContain('needs a public HTTPS URL');
			}
		});

		it('does not flag a public host that merely starts like a private range', () => {
			// 172.15.x and 172.32.x are public (private block is 172.16–31 only).
			for (const mcp of [
				'https://172.15.0.1/mcp',
				'https://172.32.0.1/mcp',
				'https://11.0.0.1/mcp',
				'https://100.63.0.1/mcp', // just below CGNAT block
				'https://100.128.0.1/mcp' // just above CGNAT block
			]) {
				const opts = installOptions({ ...base, urls: { mcp, rest: null } });
				expect(byLabel(opts, 'ChatGPT').hint).not.toContain('needs a public');
			}
		});
	});

	describe('bearer auth', () => {
		const bearer: Args = { ...base, auth: 'bearer' };
		const pubBearer: Args = { ...pub, auth: 'bearer' };

		it('adds an Authorization header to the Claude Code command', () => {
			expect(byLabel(installOptions(bearer), 'Claude Code').value).toContain(
				'--header "Authorization: Bearer <YOUR_TOKEN>"'
			);
		});

		it('threads the token through mcp-remote via the env workaround (Windows-safe)', () => {
			const entry = JSON.parse(byLabel(installOptions(pubBearer), 'Claude Desktop').value);
			expect(entry.mcpServers.memory).toEqual({
				command: 'npx',
				args: ['-y', 'mcp-remote', PUBLIC_MCP, '--header', 'Authorization:${AUTH_HEADER}'],
				env: { AUTH_HEADER: 'Bearer <YOUR_TOKEN>' }
			});
		});

		it('keeps --allow-http alongside the bearer header for local endpoints', () => {
			const entry = JSON.parse(byLabel(installOptions(bearer), 'Claude Desktop').value);
			expect(entry.mcpServers.memory.args).toEqual([
				'-y',
				'mcp-remote',
				base.urls.mcp,
				'--allow-http',
				'--header',
				'Authorization:${AUTH_HEADER}'
			]);
		});

		it('warns that URL-only connectors do not support bearer auth (public URL)', () => {
			const opts = installOptions(pubBearer);
			expect(byLabel(opts, 'Claude web / mobile').hint).toContain('bearer auth not supported');
			expect(byLabel(opts, 'ChatGPT').hint).toContain('bearer auth not supported');
		});

		it('omits the bearer warning for none-auth servers', () => {
			const opts = installOptions(pub);
			expect(byLabel(opts, 'Claude web / mobile').hint).not.toContain('bearer');
			expect(byLabel(opts, 'ChatGPT').hint).not.toContain('bearer');
		});

		it('surfaces both caveats when a server is local AND bearer-protected', () => {
			// default base URL is local http; bearer adds the second blocker
			const hint = byLabel(installOptions(bearer), 'ChatGPT').hint ?? '';
			expect(hint).toContain('needs a public HTTPS URL');
			expect(hint).toContain('bearer auth not supported here');
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

	it('lets OAuth-capable clients discover auth without a static bearer header', () => {
		const opts = installOptions({ ...pub, auth: 'oauth' });
		expect(byLabel(opts, 'Claude Code').value).not.toContain('--header');
		expect(byLabel(opts, 'Claude web / mobile').hint).not.toContain('bearer auth not supported');
		expect(JSON.parse(byLabel(opts, 'mcpServers').value).mcpServers.memory.headers).toBeUndefined();
	});
});
