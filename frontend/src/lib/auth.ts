// The control-plane admin token, persisted in localStorage.
//
// localStorage is the single source of truth. The layout re-reads it on
// navigation and api.ts reads it per request, so there's no separate reactive
// mirror that could drift. All three accessors are no-ops when localStorage is
// unavailable (SSR / pre-hydration), so importing this never throws.

const KEY = 'mcpe_admin_token';

/** The stored admin token, or null when none is set (or storage is unavailable). */
export function getToken(): string | null {
	if (typeof localStorage === 'undefined') return null;
	return localStorage.getItem(KEY);
}

export function setToken(token: string): void {
	if (typeof localStorage === 'undefined') return;
	localStorage.setItem(KEY, token);
}

export function clearToken(): void {
	if (typeof localStorage === 'undefined') return;
	localStorage.removeItem(KEY);
}

// A stored token rides every request as Authorization, and the backend then judges
// the request on it alone, ignoring a Sign-in session. So SSO must never leave one
// behind: starting SSO drops it, and so does landing from a successful callback.

// Set by the backend's successful SSO callback (app.auth.sign_in.LANDED_COOKIE).
const LANDED = 'mcpe_sso_landed';

/** Begin SSO sign-in, landing on `next` afterwards. */
export function startSsoSignIn(
	next: string,
	go: (url: string) => void = (url) => window.location.assign(url)
): void {
	clearToken();
	go(`/api/auth/oidc/login?next=${encodeURIComponent(next)}`);
}

/** Call on every page load: after a successful SSO callback, drop any stored token
 * (one set in another tab meanwhile, say) so the new session is what's used. */
export function adoptSsoLanding(): void {
	if (typeof document === 'undefined') return;
	if (!document.cookie.split('; ').some((c) => c.startsWith(`${LANDED}=`))) return;
	clearToken();
	document.cookie = `${LANDED}=; Max-Age=0; Path=/`;
}
