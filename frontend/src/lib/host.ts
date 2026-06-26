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
