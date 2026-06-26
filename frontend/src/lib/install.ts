// Per-client "add this server" snippets, derived from an elevated server's URLs.
// The proxy speaks Streamable HTTP, so every snippet is the remote-HTTP form.
// Note: we use `type: "http"` in JSON — the `streamable-http` alias breaks the
// Cursor CLI parser, while `http` is accepted everywhere.

import type { ServerSummary } from './types';

export type InstallKind = 'url' | 'cmd' | 'json';

export interface InstallOption {
	label: string;
	value: string;
	kind: InstallKind;
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
		const jsonEntry = bearer
			? { type: 'http', url: mcp, headers: { Authorization: 'Bearer <YOUR_TOKEN>' } }
			: { type: 'http', url: mcp };

		out.push({ kind: 'url', label: 'MCP URL', value: mcp });
		out.push({
			kind: 'cmd',
			label: 'Claude Code',
			value: `claude mcp add --transport http${claudeAuth} ${qName} ${qMcp}`
		});
		out.push({
			kind: 'cmd',
			label: 'Codex',
			value: bearer
				? `codex mcp add ${qName} --url ${qMcp} --bearer-token-env-var MCPE_TOKEN`
				: `codex mcp add ${qName} --url ${qMcp}`
		});
		out.push({
			kind: 'json',
			label: 'mcpServers',
			value: JSON.stringify({ mcpServers: { [name]: jsonEntry } }, null, 2)
		});
		out.push({
			kind: 'json',
			label: 'VS Code',
			value: JSON.stringify({ servers: { [name]: jsonEntry } }, null, 2)
		});
	}
	if (rest) {
		out.push({ kind: 'url', label: 'REST URL', value: rest });
	}
	return out;
}
