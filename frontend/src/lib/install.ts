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

export function installOptions(server: Pick<ServerSummary, 'slug' | 'urls'>): InstallOption[] {
	const { mcp, rest } = server.urls;
	const name = server.slug;
	const out: InstallOption[] = [];

	if (mcp) {
		const qName = shellQuote(name);
		const qMcp = shellQuote(mcp);
		out.push({ kind: 'url', label: 'MCP URL', value: mcp });
		out.push({
			kind: 'cmd',
			label: 'Claude Code',
			value: `claude mcp add --transport http ${qName} ${qMcp}`
		});
		out.push({ kind: 'cmd', label: 'Codex', value: `codex mcp add ${qName} --url ${qMcp}` });
		out.push({
			kind: 'json',
			label: 'mcpServers',
			value: JSON.stringify({ mcpServers: { [name]: { type: 'http', url: mcp } } }, null, 2)
		});
		out.push({
			kind: 'json',
			label: 'VS Code',
			value: JSON.stringify({ servers: { [name]: { type: 'http', url: mcp } } }, null, 2)
		});
	}
	if (rest) {
		out.push({ kind: 'url', label: 'REST URL', value: rest });
	}
	return out;
}
