// Per-client "add this server" snippets, derived from an elevated server's URLs.
// The proxy speaks Streamable HTTP, so every snippet is the remote-HTTP form.
// Note: we use `type: "http"` in JSON — the `streamable-http` alias breaks the
// Cursor CLI parser, while `http` is accepted everywhere.

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

export function installOptions(server: Pick<ServerSummary, 'slug' | 'urls' | 'auth'>): InstallOption[] {
	const { mcp, rest } = server.urls;
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
		// Claude Desktop's config validates stdio entries only — a bare `url` is
		// dropped — so remote servers go through the `mcp-remote` stdio bridge.
		const remoteArgs = bearer
			? ['mcp-remote', mcp, '--header', 'Authorization: Bearer <YOUR_TOKEN>']
			: ['mcp-remote', mcp];
		const desktopEntry = { command: 'npx', args: remoteArgs };
		// Gemini CLI uses `httpUrl` (not `url`/`type`) for Streamable HTTP.
		const geminiEntry = bearer
			? { httpUrl: mcp, headers: { Authorization: 'Bearer <YOUR_TOKEN>' } }
			: { httpUrl: mcp };
		// URL-only connector UIs (claude.ai, ChatGPT) take just the endpoint and
		// handle auth via their own OAuth / no-auth flow — a static token can't be
		// attached, so for bearer servers paste the URL into a `none`-auth server.
		const connectorHint = bearer
			? 'Paste this URL into the connector UI (bearer auth not supported there)'
			: 'Paste this URL into the connector UI';

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
			hint: 'Settings → Connectors → Add custom connector',
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
			hint: 'Developer mode → Settings → Connectors → Create',
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
	if (rest) {
		out.push({ kind: 'url', group: 'Generic', label: 'REST URL', value: rest });
	}
	return out;
}
