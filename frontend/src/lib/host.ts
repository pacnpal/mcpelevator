// Pure host-classification helpers for the settings page's self-lockout guard
// rails. Kept free of Svelte/DOM so the logic stays verifiable in isolation and
// mirrors the backend control-plane allowlist (`app/auth/middleware.py`) exactly.

/**
 * Loopback hostnames the control plane *always* allows, mirroring the backend's
 * `_LOOPBACK` set. A custom hostname that merely resolves to 127.0.0.1 (e.g.
 * `myapp.local`) is deliberately NOT loopback here — the backend compares literal
 * hostnames, so the guard must agree or it would mis-judge lockout risk.
 */
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1']);

/**
 * Normalize a hostname for comparison: trim, lowercase, and strip the surrounding
 * brackets browsers add to IPv6 literals (`window.location.hostname` is `"[::1]"`,
 * but the backend's allowlist holds the bare `"::1"`).
 */
export function normalizeHost(host: string): string {
	const trimmed = host.trim().toLowerCase();
	if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
		return trimmed.slice(1, -1);
	}
	return trimmed;
}

/**
 * True when `host` is a loopback hostname the control plane always allows, so
 * neither revoking the allowlist nor switching to `local` can lock it out.
 */
export function isLoopbackHost(host: string): boolean {
	return LOOPBACK_HOSTS.has(normalizeHost(host));
}

/**
 * True when `host` is a private-network IP *literal* (RFC 1918 / link-local /
 * IPv6 ULA / loopback). Mirrors the backend's `_is_private_host_literal`, used by
 * the settings page to warn before disabling `allow_private_lan` would lock out a
 * browser reaching the box through such an address. Advisory only — loopback always
 * recovers — so it favours correctness on the common ranges over exhaustiveness.
 */
export function isPrivateIpHost(host: string): boolean {
	const h = normalizeHost(host);
	if (LOOPBACK_HOSTS.has(h)) return true;
	// IPv4 dotted quad in a private range.
	const v4 = h.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
	if (v4) {
		const [a, b, c, d] = v4.slice(1).map(Number);
		if ([a, b, c, d].some((n) => n > 255)) return false;
		if (a === 10) return true; // 10.0.0.0/8
		if (a === 127) return true; // 127.0.0.0/8 (loopback)
		if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12
		if (a === 192 && b === 168) return true; // 192.168.0.0/16
		if (a === 169 && b === 254) return true; // 169.254.0.0/16 link-local
		return false;
	}
	// IPv6 unique-local (fc00::/7) or link-local (fe80::/10).
	if (h.includes(':')) {
		return /^f[cd]/.test(h) || /^fe[89ab]/.test(h);
	}
	return false;
}
