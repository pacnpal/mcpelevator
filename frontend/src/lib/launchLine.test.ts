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

	it('parses uvx lines', () => {
		expect(parseLaunchLine('  uvx mcp-server-time --local-timezone=UTC ')).toEqual({
			runner: 'uvx',
			pkg: 'mcp-server-time',
			extra: ['--local-timezone=UTC']
		});
	});

	it('joins backslash-continued lines', () => {
		expect(parseLaunchLine('npx -y pkg \\\n  --flag')?.extra).toEqual(['--flag']);
	});

	it('refuses shapes it cannot represent', () => {
		expect(parseLaunchLine('@scope/pkg')).toBeNull(); // just a package: paste as typed
		expect(parseLaunchLine('uvx --from git+https://x tool')).toBeNull();
		expect(parseLaunchLine('npx -y pkg | tee log')).toBeNull();
		expect(parseLaunchLine('npx -y "pkg')).toBeNull();
		expect(parseLaunchLine('npx -y')).toBeNull();
		expect(parseLaunchLine('npx a\nnpx b')).toBeNull();
	});
});

describe('shellSplit', () => {
	it('handles quotes and escapes', () => {
		expect(shellSplit(`a 'b c' "d \\"e\\"" f\\ g`)).toEqual(['a', 'b c', 'd "e"', 'f g']);
		expect(shellSplit(`x ''`)).toEqual(['x', '']);
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
