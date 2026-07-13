/**
 * Popup-window plumbing for the upstream-OAuth sign-in round trip.
 *
 * The provider sign-in used to take over the whole tab: navigate to the authorize URL,
 * bounce through `/api/oauth/callback`, land back on the SPA with `?oauth=…`. Losing the
 * page (and any in-progress state) to an external site is jarring, so the flow now runs
 * in a popup: the server page opens one synchronously in the click gesture (popup
 * blockers only allow `window.open` there), points it at the authorize URL, and listens
 * for the result. The callback still redirects to the SPA — but *inside the popup* —
 * where the root layout forwards the result to the opener via `postMessage` and closes
 * the popup. If the popup is blocked, callers fall back to the old full-tab navigation,
 * which the `?oauth=…` toast path still handles.
 */

const MESSAGE_TYPE = 'mcpelevator:oauth';
const WINDOW_NAME = 'mcpelevator-oauth';

export interface OauthResult {
	result: 'connected' | 'error';
	reason: string | null;
}

/** Open the (blank) sign-in popup, centered on the current window. Must be called
 * synchronously from a user gesture; returns null when a popup blocker eats it. */
export function openOauthPopup(): Window | null {
	const width = 600;
	const height = 720;
	const left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
	const top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);
	return window.open(
		'about:blank',
		WINDOW_NAME,
		`popup=yes,width=${width},height=${height},left=${Math.round(left)},top=${Math.round(top)}`
	);
}

/** Subscribe to OAuth results forwarded from the popup. Same-origin messages only.
 * Returns an unsubscribe function (hand it to an `$effect` teardown). */
export function listenForOauthResult(onResult: (result: OauthResult) => void): () => void {
	const handler = (event: MessageEvent) => {
		if (event.origin !== window.location.origin) return;
		const data: unknown = event.data;
		if (typeof data !== 'object' || data === null) return;
		const message = data as { type?: unknown; result?: unknown; reason?: unknown };
		if (message.type !== MESSAGE_TYPE) return;
		if (message.result !== 'connected' && message.result !== 'error') return;
		onResult({
			result: message.result,
			reason: typeof message.reason === 'string' ? message.reason : null
		});
	};
	window.addEventListener('message', handler);
	return () => window.removeEventListener('message', handler);
}

/** Called from the root layout on every navigation: when this document is the sign-in
 * popup landing back from `/api/oauth/callback` (it has an opener and `?oauth=…` in the
 * URL), forward the result to the opener and close. Harmless when this is a regular
 * tab that merely has an opener: the message targets our own origin (nobody listening →
 * no-op) and `window.close()` is ignored for windows a script didn't open, so the page's
 * normal `?oauth=…` toast handling still runs. */
export function forwardOauthResultToOpener(url: URL): void {
	const result = url.searchParams.get('oauth');
	if (result !== 'connected' && result !== 'error') return;
	const opener: Window | null = window.opener;
	if (!opener || opener.closed) return;
	try {
		opener.postMessage(
			{ type: MESSAGE_TYPE, result, reason: url.searchParams.get('reason') },
			window.location.origin
		);
	} catch {
		return; // opener gone mid-flight; the fallback toast path takes over
	}
	window.close();
}
