import { describe, expect, it } from 'vitest';

import { isLoopbackHost, isPrivateIpHost, normalizeHost } from './host';

describe('normalizeHost', () => {
	it('trims, lowercases, and strips IPv6 brackets', () => {
		expect(normalizeHost('  LocalHost ')).toBe('localhost');
		expect(normalizeHost('[::1]')).toBe('::1');
		expect(normalizeHost('[FD00::1]')).toBe('fd00::1');
	});
});

describe('isLoopbackHost', () => {
	it('matches only literal loopback hosts', () => {
		expect(isLoopbackHost('localhost')).toBe(true);
		expect(isLoopbackHost('127.0.0.1')).toBe(true);
		expect(isLoopbackHost('[::1]')).toBe(true);
		// a name that merely resolves to loopback is not loopback here
		expect(isLoopbackHost('myapp.local')).toBe(false);
		expect(isLoopbackHost('192.168.1.5')).toBe(false);
	});
});

describe('isPrivateIpHost', () => {
	it('accepts RFC 1918 / link-local / loopback IPv4 literals', () => {
		expect(isPrivateIpHost('10.0.0.5')).toBe(true);
		expect(isPrivateIpHost('172.16.4.4')).toBe(true);
		expect(isPrivateIpHost('172.31.255.255')).toBe(true);
		expect(isPrivateIpHost('192.168.1.50')).toBe(true);
		expect(isPrivateIpHost('169.254.1.1')).toBe(true);
		expect(isPrivateIpHost('127.0.0.1')).toBe(true);
	});

	it('accepts IPv6 ULA / link-local literals', () => {
		expect(isPrivateIpHost('fd00::1')).toBe(true);
		expect(isPrivateIpHost('[fd12:3456::1]')).toBe(true);
		expect(isPrivateIpHost('fe80::1')).toBe(true);
	});

	it('rejects public IPs, out-of-range octets, and hostnames', () => {
		expect(isPrivateIpHost('8.8.8.8')).toBe(false);
		expect(isPrivateIpHost('172.15.0.1')).toBe(false); // just below the /12
		expect(isPrivateIpHost('172.32.0.1')).toBe(false); // just above the /12
		expect(isPrivateIpHost('192.169.0.1')).toBe(false);
		expect(isPrivateIpHost('999.1.1.1')).toBe(false); // invalid octet
		expect(isPrivateIpHost('nas.local')).toBe(false); // hostname, not a literal
		expect(isPrivateIpHost('2001:db8::1')).toBe(false); // public/documentation IPv6
	});
});
