/**
 * Parse a pasted `npx …` / `uvx …` launch line into the add form's friendly fields.
 *
 * READMEs hand people a whole command (`npx -y @scope/server-x --flag`), and the
 * natural move is to paste it into the package field. Only the simple, unambiguous
 * shapes are recognised: the runner word, npx's own `-y`/`--yes`, then the package,
 * then the server's arguments. Anything else (options before the package, a shell
 * pipeline, an unclosed quote) returns null and the paste lands as typed.
 */
export interface LaunchLine {
	runner: 'npx' | 'uvx';
	pkg: string;
	extra: string[];
}

/** Split like a POSIX shell for plain words and single/double quotes; null if unbalanced. */
export function shellSplit(line: string): string[] | null {
	const out: string[] = [];
	let cur = '';
	let has = false;
	let quote: '"' | "'" | null = null;
	for (let i = 0; i < line.length; i++) {
		const ch = line[i];
		if (quote) {
			if (ch === quote) quote = null;
			// In double quotes a backslash escapes only $ ` " \ (POSIX); before
			// anything else it stays literal ("\d+", "C:\tmp").
			else if (ch === '\\' && quote === '"' && '$`"\\'.includes(line[i + 1] ?? '')) cur += line[++i];
			else cur += ch;
		} else if (ch === '"' || ch === "'") {
			quote = ch;
			has = true;
		} else if (ch === '\\' && i + 1 < line.length) {
			cur += line[++i];
			has = true;
		} else if (/\s/.test(ch)) {
			if (has) out.push(cur);
			cur = '';
			has = false;
		} else {
			cur += ch;
			has = true;
		}
	}
	if (quote) return null;
	if (has) out.push(cur);
	return out;
}

const SHELL_META = /[|&;<>`$()]/;

export function parseLaunchLine(text: string): LaunchLine | null {
	const line = text.trim().replace(/\\\r?\n\s*/g, ' '); // join backslash-continued lines
	if (!/^(npx|uvx)\s/.test(line) || line.includes('\n')) return null;
	const words = shellSplit(line);
	if (!words || words.some((w) => SHELL_META.test(w) && !w.startsWith('--'))) return null;
	const runner = words[0] as 'npx' | 'uvx';
	let i = 1;
	if (runner === 'npx') while (words[i] === '-y' || words[i] === '--yes') i++;
	const pkg = words[i];
	if (!pkg || pkg.startsWith('-')) return null;
	const extra = words.slice(i + 1);
	// The form keeps one trimmed argument per line, so an empty or space-padded
	// argument can't survive the split; leave that paste as typed.
	if (extra.some((a) => a === '' || a !== a.trim() || a.includes('\n'))) return null;
	return { runner, pkg, extra };
}

/** A starting name from a package: `@scope/server-memory@1.2` -> `server-memory`. */
export function nameFromPackage(pkg: string): string {
	const bare = pkg.replace(/^@[^/]+\//, '').replace(/@[^@]*$/, '').replace(/\[.*\]$/, '');
	return bare.split(/[/=]/).pop() || pkg;
}
