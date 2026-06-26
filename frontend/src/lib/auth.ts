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
