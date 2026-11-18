import { describe, expect, it } from 'vitest';
import { endpointReach } from './endpoint';

describe('endpointReach', () => {
	it.each([
		'http://localhost:8080/s/memory/mcp',
		'http://127.0.0.1:8080/s/memory/mcp',
		'http://192.168.1.20:8080/s/memory/mcp',
		'http://10.0.0.5/s/x/mcp',
		'http://172.20.1.1/s/x/mcp',
		'http://100.101.102.103/s/x/mcp',
		'https://tower.local/s/x/mcp',
		'http://tower/s/x/mcp',
		'http://[::1]:8080/s/x/mcp',
		'https://[fd12:3456::1]/s/x/mcp',
		'https://localhost./s/x/mcp',
		'https://tower.local./s/x/mcp',
		'https://[::ffff:127.0.0.1]/s/x/mcp',
		'https://[::ffff:192.168.1.20]/s/x/mcp'
	])('treats %s as local', (url) => {
		expect(endpointReach(url)).toBe('local');
	});

	it('flags a public http URL as insecure', () => {
		expect(endpointReach('http://mcp.example.com/s/x/mcp')).toBe('insecure');
		expect(endpointReach('http://203.0.113.9/s/x/mcp')).toBe('insecure');
	});

	it('accepts https on a public host', () => {
		expect(endpointReach('https://mcp.example.com/s/x/mcp')).toBe('public');
		expect(endpointReach('https://172.32.0.1/s/x/mcp')).toBe('public');
		expect(endpointReach('https://mcp.example.com./s/x/mcp')).toBe('public');
		expect(endpointReach('https://[::ffff:203.0.113.9]/s/x/mcp')).toBe('public');
	});

	it('returns null for missing or unparseable URLs', () => {
		expect(endpointReach(null)).toBeNull();
		expect(endpointReach('')).toBeNull();
		expect(endpointReach('not a url')).toBeNull();
	});
});
