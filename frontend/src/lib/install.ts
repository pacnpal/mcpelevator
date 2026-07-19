// Per-client "add this server" snippets, derived from an elevated server's URLs.
// The proxy speaks Streamable HTTP, so every snippet is the remote-HTTP form.
// Note: we use `type: "http"` in JSON — the `streamable-http` alias breaks the
// Cursor CLI parser, while `http` is accepted everywhere.

import { isPrivateIpHost } from './host';
import type { ServerSummary } from './types';

export type InstallKind = 'url' | 'cmd' | 'json';

// Menu groups, in display order. Snippets are bucketed by ecosystem so the
// (now longer) list stays scannable.
export type InstallGroup = 'Claude' | 'OpenAI' | 'Google' | 'Editors' | 'Generic';

export const INSTALL_GROUP_ORDER: InstallGroup[] = ['Claude', 'OpenAI', 'Google', 'Editors', 'Generic'];

export interface InstallOption {
	label: string;
	value: string;
	kind: InstallKind;
	group: InstallGroup;
	/** Short "what to do with this" hint, shown under the label in the menu. */
	hint?: string;
}

// POSIX shell-quote an argument for the pasteable `claude`/`codex` commands, so a
// URL/name with spaces, &, quotes, or $() can't break or alter what the shell runs.
function shellQuote(value: string): string {
	if (/^[A-Za-z0-9_./:@%+=,-]+$/.test(value)) return value;
	return `'${value.replace(/'/g, `'\\''`)}'`;
}

// A loopback / private / link-local host that a vendor cloud can't dial. Extends the
// backend-mirroring classifier in `host.ts` with ranges that only matter for
// reachability: 0.0.0.0/8 and CGNAT (100.64.0.0/10, e.g. Tailscale).
function isPrivateHost(host: string): boolean {
	const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
	if (v4) {
		const [a, b, c, d] = v4.slice(1).map(Number);
		if ([a, b, c, d].some((n) => n > 255)) return false;
		if (a === 0 || (a === 100 && b >= 64 && b <= 127)) return true;
	}
	return isPrivateIpHost(host);
}

// Whether a URL is not reachable from the public internet — plain http, or an
// http(s) loopback / private / link-local host. Cloud connector UIs (claude.ai,
// ChatGPT) dial the server from Anthropic/OpenAI infrastructure, so a local
// default like `http://127.0.0.1:8080/...` or a LAN/VPC address like
// `https://192.168.1.10` can't be reached; mcp-remote also needs `--allow-http`
// for non-https endpoints.
function isLocalUrl(value: string): boolean {
	let url: URL;
	try {
		url = new URL(value);
	} catch {
		return false;
	}
	if (url.protocol !== 'https:') return true; // http:// — insecure and (for ChatGPT) rejected
	const host = url.hostname.replace(/^\[|\]$/g, ''); // strip IPv6 brackets
	return (
		host === 'localhost' ||
		host.endsWith('.local') ||
		host.endsWith('.localhost') ||
		isPrivateHost(host)
	);
}

export function installOptions(server: Pick<ServerSummary, 'slug' | 'urls' | 'auth'>): InstallOption[] {
	const { mcp } = server.urls;
	const name = server.slug;
	const bearer = server.auth === 'bearer';
	const out: InstallOption[] = [];

	if (mcp) {
		const qName = shellQuote(name);
		const qMcp = shellQuote(mcp);
		// Bearer-protected servers reject unauthenticated clients, so each snippet
		// carries the Authorization header with a placeholder for the user's token
		// (tokens are shown once at creation and can't be read back here).
		const claudeAuth = bearer ? ' --header "Authorization: Bearer <YOUR_TOKEN>"' : '';
		// Streamable-HTTP entry for clients that speak remote MCP natively
		// (mcpServers, VS Code, Codex JSON, etc.).
		const httpEntry = bearer
			? { type: 'http', url: mcp, headers: { Authorization: 'Bearer <YOUR_TOKEN>' } }
			: { type: 'http', url: mcp };
		const local = isLocalUrl(mcp);
		// Claude Desktop's config validates stdio entries only — a bare `url` is
		// dropped — so remote servers go through the `mcp-remote` stdio bridge.
		// `--allow-http` is required for non-https endpoints (the default local
		// install advertises an http loopback URL). `-y` auto-accepts the npx
		// install prompt, which Claude Desktop (launching npx as a stdio server)
		// can't answer on a machine that hasn't cached mcp-remote yet.
		const remoteArgs = ['-y', 'mcp-remote', mcp];
		if (local) remoteArgs.push('--allow-http');
		// For the bearer header we use mcp-remote's documented `${VAR}` form with an
		// `env` value instead of an inline `--header "Authorization: Bearer …"`:
		// Claude Desktop on Windows mangles spaces inside an arg, which would
		// corrupt the header. The env form keeps the arg space-free and works on
		// every platform. See github.com/geelen/mcp-remote (Custom Headers).
		const desktopEntry = bearer
			? {
					command: 'npx',
					args: [...remoteArgs, '--header', 'Authorization:${AUTH_HEADER}'],
					env: { AUTH_HEADER: 'Bearer <YOUR_TOKEN>' }
				}
			: { command: 'npx', args: remoteArgs };
		// Gemini CLI uses `httpUrl` (not `url`/`type`) for Streamable HTTP.
		const geminiEntry = bearer
			? { httpUrl: mcp, headers: { Authorization: 'Bearer <YOUR_TOKEN>' } }
			: { httpUrl: mcp };
		// URL-only connector UIs (claude.ai, ChatGPT) dial the server from the
		// vendor's cloud, so a local URL is unreachable there; and they handle auth
		// via their own OAuth / no-auth flow, so a static bearer token can't be
		// attached. These are independent blockers — surface every one that applies,
		// so fixing reachability doesn't hide the still-present auth problem.
		const connectorCaveats: string[] = [];
		if (local) connectorCaveats.push('needs a public HTTPS URL');
		if (bearer) connectorCaveats.push('bearer auth not supported here');
		const connectorCaveat = connectorCaveats.length ? ` · ${connectorCaveats.join(' · ')}` : '';

		// — Claude —
		out.push({
			kind: 'cmd',
			group: 'Claude',
			label: 'Claude Code',
			value: `claude mcp add --transport http${claudeAuth} ${qName} ${qMcp}`
		});
		out.push({
			kind: 'json',
			group: 'Claude',
			label: 'Claude Desktop',
			hint: 'Add to claude_desktop_config.json (via mcp-remote)',
			value: JSON.stringify({ mcpServers: { [name]: desktopEntry } }, null, 2)
		});
		out.push({
			kind: 'url',
			group: 'Claude',
			label: 'Claude web / mobile',
			hint: `Settings → Connectors → Add custom connector${connectorCaveat}`,
			value: mcp
		});

		// — OpenAI —
		out.push({
			kind: 'cmd',
			group: 'OpenAI',
			label: 'Codex',
			value: bearer
				? `codex mcp add ${qName} --url ${qMcp} --bearer-token-env-var MCPE_TOKEN`
				: `codex mcp add ${qName} --url ${qMcp}`
		});
		out.push({
			kind: 'url',
			group: 'OpenAI',
			label: 'ChatGPT',
			hint: `Developer mode → Settings → Connectors → Create${connectorCaveat}`,
			value: mcp
		});

		// — Google —
		out.push({
			kind: 'json',
			group: 'Google',
			label: 'Gemini CLI',
			hint: 'Add to .gemini/settings.json',
			value: JSON.stringify({ mcpServers: { [name]: geminiEntry } }, null, 2)
		});

		// — Editors —
		out.push({
			kind: 'json',
			group: 'Editors',
			label: 'VS Code',
			value: JSON.stringify({ servers: { [name]: httpEntry } }, null, 2)
		});

		// — Generic —
		out.push({
			kind: 'json',
			group: 'Generic',
			label: 'mcpServers',
			hint: 'Cursor, Windsurf, and other mcpServers clients',
			value: JSON.stringify({ mcpServers: { [name]: httpEntry } }, null, 2)
		});
		out.push({ kind: 'url', group: 'Generic', label: 'MCP URL', value: mcp });
	}
	// The per-server REST/OpenAPI surface isn't served yet (planned, M6), so it's not
	// offered here even if a (legacy) server still has rest_openapi set.
	return out;
}
