import { describe, expect, it } from 'vitest';
import { nameFromPackage, parseLaunchLine, shellSplit } from './launchLine';

describe('parseLaunchLine', () => {
	it('splits an npx line into package and extra args', () => {
		expect(parseLaunchLine('npx -y @modelcontextprotocol/server-filesystem /data "/my docs"')).toEqual({
			runner: 'npx',
			pkg: '@modelcontextprotocol/server-filesystem',
			extra: ['/data', '/my docs']
		});
	});

	it('accepts npx without -y and with --yes', () => {
		expect(parseLaunchLine('npx some-server')?.pkg).toBe('some-server');
		expect(parseLaunchLine('npx --yes some-server --port 8000')).toEqual({
			runner: 'npx',
			pkg: 'some-server',
			extra: ['--port', '8000']
		});
	});

	it('keeps metacharacters that are quoted or escaped', () => {
		expect(parseLaunchLine(`npx pkg --pattern='a|b' --price='$5' --sep="a;b" \\$HOME`)?.extra).toEqual([
			'--pattern=a|b',
			'--price=$5',
			'--sep=a;b',
			'$HOME'
		]);
		expect(parseLaunchLine(`npx pkg "~/Desktop" \\~/x '*.md' a~b x#y`)?.extra).toEqual([
			'~/Desktop',
			'~/x',
			'*.md',
			'a~b',
			'x#y'
		]);
	});

	it('parses uvx lines', () => {
		expect(parseLaunchLine('  uvx mcp-server-time --local-timezone=UTC ')).toEqual({
			runner: 'uvx',
			pkg: 'mcp-server-time',
			extra: ['--local-timezone=UTC']
		});
	});

	it('joins backslash-continued lines', () => {
		expect(parseLaunchLine('npx -y pkg \\\n  --flag')?.extra).toEqual(['--flag']);
		expect(parseLaunchLine('npx -y pkg \\\r\n  --flag')?.extra).toEqual(['--flag']);
		// A continuation inside a word joins it, with no argument break (POSIX).
		expect(parseLaunchLine('npx pkg --path=/srv/\\\ndata')?.extra).toEqual(['--path=/srv/data']);
		expect(parseLaunchLine('npx pkg "--path=/srv/\\\ndata"')?.extra).toEqual(['--path=/srv/data']);
	});

	it('refuses shapes it cannot represent', () => {
		expect(parseLaunchLine('@scope/pkg')).toBeNull(); // just a package: paste as typed
		expect(parseLaunchLine('uvx --from git+https://x tool')).toBeNull();
		expect(parseLaunchLine('npx -y pkg | tee log')).toBeNull();
		expect(parseLaunchLine('npx -y "pkg')).toBeNull();
		expect(parseLaunchLine('npx -y')).toBeNull();
		expect(parseLaunchLine('npx a\nnpx b')).toBeNull();
		// The form stores one trimmed arg per line: these can't round-trip.
		expect(parseLaunchLine('npx pkg --prefix "" --verbose')).toBeNull();
		expect(parseLaunchLine('npx pkg --name " padded "')).toBeNull();
		// Nothing expands args, so shell syntax anywhere (options included) is refused.
		expect(parseLaunchLine('npx pkg --api-key=$API_KEY')).toBeNull();
		expect(parseLaunchLine('npx pkg --token="$TOKEN"')).toBeNull();
		expect(parseLaunchLine('npx pkg --out=a>b')).toBeNull();
		expect(parseLaunchLine('npx pkg --x=`id`')).toBeNull();
		// Tilde, globs, braces and comments are shell rewrites too.
		expect(parseLaunchLine('npx -y @modelcontextprotocol/server-filesystem ~/Desktop')).toBeNull();
		expect(parseLaunchLine('npx pkg --root=~/x')).toBeNull();
		expect(parseLaunchLine('npx pkg /data/*')).toBeNull();
		expect(parseLaunchLine('npx pkg file{1,2}')).toBeNull();
		expect(parseLaunchLine('npx pkg --flag # note')).toBeNull();
	});
});

describe('shellSplit', () => {
	it('handles quotes and escapes', () => {
		expect(shellSplit(`a 'b c' "d \\"e\\"" f\\ g`)).toEqual(['a', 'b c', 'd "e"', 'f g']);
		expect(shellSplit(`x ''`)).toEqual(['x', '']);
		// Inside double quotes only $ ` " \ are escapable; other backslashes stay.
		expect(shellSplit(String.raw`--pattern="\d+" "C:\tmp\x"`)).toEqual([
			String.raw`--pattern=\d+`,
			String.raw`C:\tmp\x`
		]);
		expect(shellSplit(String.raw`"a\\b \"q\""`)).toEqual([String.raw`a\b "q"`]);
	});
});

describe('nameFromPackage', () => {
	it('drops scope, version and extras', () => {
		expect(nameFromPackage('@modelcontextprotocol/server-memory')).toBe('server-memory');
		expect(nameFromPackage('@scope/pkg@1.2.3')).toBe('pkg');
		expect(nameFromPackage('mcp-server-fetch[extra]')).toBe('mcp-server-fetch');
		expect(nameFromPackage('plain')).toBe('plain');
	});
});
