// Remote-runner transport vocabulary — the frontend SSOT, mirroring the backend's
// `app/runners/remote.py`. Both the create form and the catalog install flow
// canonicalize through here so an alias (e.g. `http`) never reaches the API or the
// <select> as an unsupported/blank value.

export const REMOTE_TRANSPORTS: { value: string; label: string }[] = [
	{ value: 'streamable-http', label: 'Streamable HTTP' },
	{ value: 'sse', label: 'SSE' }
];

export const DEFAULT_REMOTE_TRANSPORT = 'streamable-http';

const ALIASES: Record<string, string> = {
	http: 'streamable-http',
	'streamable-http': 'streamable-http',
	streamable_http: 'streamable-http',
	streamablehttp: 'streamable-http',
	sse: 'sse'
};

/** Canonical transport for a name/alias, or `null` if unsupported. Empty → default. */
export function canonicalRemoteTransport(type: string | null | undefined): string | null {
	return ALIASES[(type || DEFAULT_REMOTE_TRANSPORT).trim().toLowerCase()] ?? null;
}
