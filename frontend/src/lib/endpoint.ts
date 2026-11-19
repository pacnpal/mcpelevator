/**
 * Who can reach an endpoint URL, judged from the URL alone.
 *
 * - `local`: a loopback, private-network, link-local or `.local` host. Only this
 *   machine or LAN can connect; Claude web/mobile and other hosted clients can't.
 * - `insecure`: a public host over plain http. Reachable, but hosted clients
 *   (Claude web/mobile) refuse non-https connectors.
 * - `public`: https on a public host.
 *
 * The server's own base URL decides this, so the fix is always the same setting:
 * `MCPE_PUBLIC_BASE_URL`.
 */
export type EndpointReach = 'local' | 'insecure' | 'public';

function isPrivateIpv4(host: string): boolean {
	const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
	if (!m) return false;
	const [a, b] = [Number(m[1]), Number(m[2])];
	return (
		a === 127 || // loopback
		a === 10 ||
		a === 0 ||
		(a === 172 && b >= 16 && b <= 31) ||
		(a === 192 && b === 168) ||
		(a === 169 && b === 254) || // link-local
		(a === 100 && b >= 64 && b <= 127) // CGNAT / Tailscale
	);
}

/** `::ffff:a.b.c.d`, which the URL parser normalises to `::ffff:hhhh:hhhh`, as dotted IPv4. */
function mappedIpv4(host: string): string | null {
	const m = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(host);
	if (!m) return null;
	const [hi, lo] = [parseInt(m[1], 16), parseInt(m[2], 16)];
	return `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
}

function isLocalHost(hostname: string): boolean {
	// A terminal dot is the DNS root (`localhost.` is still loopback).
	const host = hostname.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
	if (host === 'localhost' || host.endsWith('.localhost')) return true;
	if (host.endsWith('.local') || host.endsWith('.lan') || host.endsWith('.internal')) return true;
	if (!host.includes('.') && !host.includes(':')) return true; // bare LAN hostname
	if (isPrivateIpv4(host)) return true;
	if (host.includes(':')) {
		const v4 = mappedIpv4(host);
		if (v4) return isPrivateIpv4(v4);
		// IPv6: loopback, unique-local (fc00::/7), link-local (fe80::/10).
		return host === '::1' || /^f[cd]/.test(host) || /^fe[89ab]/.test(host);
	}
	return false;
}

export function endpointReach(url: string | null | undefined): EndpointReach | null {
	if (!url) return null;
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return null;
	}
	if (isLocalHost(parsed.hostname)) return 'local';
	return parsed.protocol === 'https:' ? 'public' : 'insecure';
}
