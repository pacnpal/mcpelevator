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
 * Design details:
 *
 * - The popup's `window.opener` is disowned before it ever leaves our origin. A page
 *   can't *read* a cross-origin opener, but it can still *navigate* it — a malicious or
 *   compromised provider could swap the control-plane tab for a look-alike (reverse
 *   tabnabbing). With no opener, the result comes back over a `BroadcastChannel`, which
 *   is same-origin by construction.
 * - Every flow gets a NONCE, written into the popup's `sessionStorage` while it is still
 *   same-origin `about:blank`. Session storage is scoped to the tab + origin, so it
 *   survives the excursion through the provider (unlike `window.name`, which browsers
 *   clear on cross-origin navigation) and is invisible to every other tab. The landing
 *   page uses its presence to recognise that it's the sign-in popup, and broadcasts it
 *   with the result so only the tab that started THIS flow reacts — a channel shared by
 *   several open control-plane tabs stays untangled. The nonce also names the popup
 *   window, so concurrent sign-ins from different tabs can't repurpose each other's
 *   popups.
 * - The popup relay only works when the callback lands back on THIS origin. When
 *   `MCPE_PUBLIC_BASE_URL` points the callback at a different origin than the tab the
 *   operator is browsing from, the marker and channel can't cross — callers detect that
 *   up front via `popupCanRelay` and use the full-tab flow instead.
 */

const CHANNEL_NAME = 'mcpelevator:oauth';
const POPUP_MARKER = 'mcpelevator:oauth-popup';

export interface OauthResult {
	result: 'connected' | 'error';
	reason: string | null;
	/** Flow nonce echoed from the popup marker; listeners drop results they didn't start. */
	nonce: string | null;
}

export interface OauthPopupHandle {
	popup: Window;
	nonce: string;
}

function newNonce(): string {
	return typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** Open the (blank) sign-in popup, centered on the current window, disowned and marked
 * with a fresh flow nonce (see module docs). Must be called synchronously from a user
 * gesture; returns null when a popup blocker eats it. */
export function openOauthPopup(): OauthPopupHandle | null {
	const width = 600;
	const height = 720;
	const left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
	const top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);
	const nonce = newNonce();
	let popup: Window | null;
	try {
		popup = window.open(
			'about:blank',
			`mcpelevator-oauth-${nonce}`,
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
		popup.sessionStorage.setItem(POPUP_MARKER, nonce);
	} catch {
		// Disown/marker failed (exotic policy): the sign-in still works — the popup
		// just won't self-close, and the popup-closed watcher resets the UI instead.
	}
	return { popup, nonce };
}

/** Whether the popup relay can work for this authorize URL: its `redirect_uri` (the
 * control plane's callback) must be on THIS origin, or the popup will land somewhere
 * our sessionStorage marker and BroadcastChannel can't reach (an operator browsing via
 * LAN/localhost while `MCPE_PUBLIC_BASE_URL` names a public origin). Callers should use
 * the full-tab flow when this is false. Unparseable input errs toward the popup. */
export function popupCanRelay(authorizeUrl: string): boolean {
	try {
		const redirect = new URL(authorizeUrl).searchParams.get('redirect_uri');
		if (!redirect) return true;
		return new URL(redirect).origin === window.location.origin;
	} catch {
		return true;
	}
}

/** Subscribe to OAuth results broadcast from the popup. Returns an unsubscribe function
 * (hand it to an `$effect` teardown). Callers must match `nonce` against the flow they
 * started — the channel is shared by every control-plane tab on this origin. */
export function listenForOauthResult(onResult: (result: OauthResult) => void): () => void {
	const channel = new BroadcastChannel(CHANNEL_NAME);
	channel.onmessage = (event: MessageEvent) => {
		const data: unknown = event.data;
		if (typeof data !== 'object' || data === null) return;
		const message = data as { result?: unknown; reason?: unknown; nonce?: unknown };
		if (message.result !== 'connected' && message.result !== 'error') return;
		onResult({
			result: message.result,
			reason: typeof message.reason === 'string' ? message.reason : null,
			nonce: typeof message.nonce === 'string' ? message.nonce : null
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
	let nonce: string | null;
	try {
		nonce = sessionStorage.getItem(POPUP_MARKER);
		if (!nonce) return;
		sessionStorage.removeItem(POPUP_MARKER);
	} catch {
		return; // no sessionStorage access → can't be the marked popup
	}
	const channel = new BroadcastChannel(CHANNEL_NAME);
	channel.postMessage({ result, reason: url.searchParams.get('reason'), nonce });
	channel.close();
	window.close();
}
