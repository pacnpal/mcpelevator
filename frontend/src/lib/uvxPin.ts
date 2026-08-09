// Client-side mirror of the backend's mcp<2 compatibility-pin placement
// (backend/app/runners/uvx.py). The pin is injected into the launch argv at
// spec-build time, never into the stored args — so every place the UI renders
// a launch command (form preview, server-detail Configuration card) must apply
// the same insertion to show what actually runs.

export const PIN_MCP1_ARGS = ['--with', 'mcp<2'];

/** Where the pin belongs in `args`, or null when placement is not certain.
 * uvx takes --with leading; a `uv` launcher only after its leading `tool run` /
 * `run` subcommand; any other executable can't take the pin at all (the backend
 * refuses to enable the toggle for those shapes — null here mirrors that). */
export function pinInsertIndex(cmd: string, args: string[]): number | null {
	const base = cmd.trim().replaceAll('\\', '/').split('/').at(-1)?.toLowerCase();
	if (base === 'uvx' || base === 'uvx.exe') return 0;
	if (base === 'uv' || base === 'uv.exe') {
		if (args[0] === 'tool' && args[1] === 'run') return 2;
		if (args[0] === 'run') return 1;
		return null;
	}
	return null;
}

/** The effective launch args: `args` with the pin inserted when it applies. */
export function withPin(cmd: string, args: string[], pinned: boolean): string[] {
	if (!pinned) return args;
	const at = pinInsertIndex(cmd, args);
	if (at === null) return args;
	return [...args.slice(0, at), ...PIN_MCP1_ARGS, ...args.slice(at)];
}

/** Quote a display token when a POSIX shell would misparse it bare — whitespace
 * or any character outside the shlex-style safe set (e.g. the `<` in "mcp<2" is
 * an input redirection unquoted) — so copied commands reproduce the real argv. */
export function quoteIfNeeded(p: string): string {
	return /^[A-Za-z0-9_@%+=:,./-]+$/.test(p) ? p : `"${p}"`;
}
