/**
 * Popup-window plumbing for the upstream-OAuth sign-in round trip.
 *
 * The provider sign-in used to take over the whole tab: navigate to the authorize URL,
 * bounce through `/api/oauth/callback`, land back on the SPA with `?oauth=…`. Losing the
 * page (and any in-progress state) to an external site is jarring, so the flow now runs
 * in a popup: the server page opens one synchronously in the click gesture (popup
 * blockers only allow `window.open` there), points it at the authorize URL, and listens
 * for the result. The callback still redirects to the SPA — but *inside the popup* —
 * where the root layout broadcasts the result to the opener tab and closes the popup.
 * If the popup is blocked, callers fall back to the old full-tab navigation, which the
 * `?oauth=…` toast path still handles.
 *
 * Two hardening details shape the design:
 *
 * - The popup's `window.opener` is disowned before it ever leaves our origin. A page
 *   can't *read* a cross-origin opener, but it can still *navigate* it — a malicious or
 *   compromised provider could swap the control-plane tab for a look-alike (reverse
 *   tabnabbing). With no opener, the result comes back over a `BroadcastChannel`, which
 *   is same-origin by construction.
 * - The landing page recognises that it's running inside the sign-in popup via a marker
 *   in `sessionStorage` — written while the popup is still same-origin `about:blank`.
 *   Session storage is scoped to the tab + origin, so the marker survives the excursion
 *   through the provider (unlike `window.name`, which browsers clear on cross-origin
 *   navigation) and is invisible to every other tab.
 */

const CHANNEL_NAME = 'mcpelevator:oauth';
const WINDOW_NAME = 'mcpelevator-oauth';
const POPUP_MARKER = 'mcpelevator:oauth-popup';

export interface OauthResult {
	result: 'connected' | 'error';
	reason: string | null;
}

/** Open the (blank) sign-in popup, centered on the current window, disowned and marked
 * (see module docs). Must be called synchronously from a user gesture; returns null when
 * a popup blocker eats it. */
export function openOauthPopup(): Window | null {
	const width = 600;
	const height = 720;
	const left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
	const top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);
	let popup: Window | null;
	try {
		popup = window.open(
			'about:blank',
			WINDOW_NAME,
			`popup=yes,width=${width},height=${height},left=${Math.round(left)},top=${Math.round(top)}`
		);
	} catch {
		// Some strict environments (sandboxed iframes, restrictive browser policies)
		// throw instead of returning null — treat both as "blocked".
		return null;
	}
	if (!popup) return null;
	try {
		popup.opener = null;
		popup.sessionStorage.setItem(POPUP_MARKER, '1');
	} catch {
		// Disown/marker failed (exotic policy): the sign-in still works — the popup
		// just won't self-close, and the popup-closed watcher resets the UI instead.
	}
	return popup;
}

/** Subscribe to OAuth results broadcast from the popup. Returns an unsubscribe function
 * (hand it to an `$effect` teardown). */
export function listenForOauthResult(onResult: (result: OauthResult) => void): () => void {
	const channel = new BroadcastChannel(CHANNEL_NAME);
	channel.onmessage = (event: MessageEvent) => {
		const data: unknown = event.data;
		if (typeof data !== 'object' || data === null) return;
		const message = data as { result?: unknown; reason?: unknown };
		if (message.result !== 'connected' && message.result !== 'error') return;
		onResult({
			result: message.result,
			reason: typeof message.reason === 'string' ? message.reason : null
		});
	};
	return () => channel.close();
}

/** Called from the root layout on every navigation: when this document is the sign-in
 * popup landing back from `/api/oauth/callback` (it carries the popup marker and
 * `?oauth=…` in the URL), broadcast the result to the opener tab and close. Regular
 * tabs never carry the marker, so their normal `?oauth=…` toast handling runs
 * untouched — and they are never closed out from under the operator. */
export function completeOauthPopup(url: URL): void {
	const result = url.searchParams.get('oauth');
	if (result !== 'connected' && result !== 'error') return;
	try {
		if (sessionStorage.getItem(POPUP_MARKER) !== '1') return;
		sessionStorage.removeItem(POPUP_MARKER);
	} catch {
		return; // no sessionStorage access → can't be the marked popup
	}
	const channel = new BroadcastChannel(CHANNEL_NAME);
	channel.postMessage({ result, reason: url.searchParams.get('reason') });
	channel.close();
	window.close();
}
