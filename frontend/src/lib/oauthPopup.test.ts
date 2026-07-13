import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
	completeOauthPopup,
	listenForOauthResult,
	openOauthPopup,
	popupCanRelay
} from './oauthPopup';

const ORIGIN = window.location.origin;
const MARKER = 'mcpelevator:oauth-popup';

/** Deterministic in-test BroadcastChannel: delivers to every other open instance with
 * the same name, synchronously (jsdom doesn't implement the real one). */
class FakeBroadcastChannel {
	static instances: FakeBroadcastChannel[] = [];
	onmessage: ((event: MessageEvent) => void) | null = null;
	closed = false;
	constructor(public name: string) {
		FakeBroadcastChannel.instances.push(this);
	}
	postMessage(data: unknown) {
		for (const other of FakeBroadcastChannel.instances) {
			if (other === this || other.closed || other.name !== this.name) continue;
			other.onmessage?.(new MessageEvent('message', { data }));
		}
	}
	close() {
		this.closed = true;
	}
}

beforeEach(() => {
	FakeBroadcastChannel.instances = [];
	vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	sessionStorage.clear();
});

describe('completeOauthPopup', () => {
	it('broadcasts the result with the flow nonce, consumes the marker, and closes', () => {
		sessionStorage.setItem(MARKER, 'nonce-1');
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));

		completeOauthPopup(new URL(`${ORIGIN}/server/abc?oauth=connected`));

		expect(seen).toEqual([{ result: 'connected', reason: null, nonce: 'nonce-1' }]);
		expect(close).toHaveBeenCalled();
		expect(sessionStorage.getItem(MARKER)).toBeNull(); // one-shot marker
		stop();
	});

	it('broadcasts the error result with its reason', () => {
		sessionStorage.setItem(MARKER, 'nonce-2');
		vi.spyOn(window, 'close').mockImplementation(() => {});
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));

		completeOauthPopup(new URL(`${ORIGIN}/?oauth=error&reason=denied`));

		expect(seen).toEqual([{ result: 'error', reason: 'denied', nonce: 'nonce-2' }]);
		stop();
	});

	it('leaves regular tabs alone: no marker means no broadcast and no close', () => {
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));

		completeOauthPopup(new URL(`${ORIGIN}/server/abc?oauth=connected`));

		expect(seen).toEqual([]);
		expect(close).not.toHaveBeenCalled();
		stop();
	});

	it('does nothing without an oauth result in the URL', () => {
		sessionStorage.setItem(MARKER, 'nonce-3');
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});

		completeOauthPopup(new URL(`${ORIGIN}/server/abc`)); // no ?oauth=
		completeOauthPopup(new URL(`${ORIGIN}/server/abc?oauth=bogus`)); // unknown value

		expect(close).not.toHaveBeenCalled();
		expect(sessionStorage.getItem(MARKER)).toBe('nonce-3'); // marker not consumed
	});
});

describe('listenForOauthResult', () => {
	it('ignores malformed broadcasts and stops after unsubscribe', () => {
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));
		const sender = new BroadcastChannel('mcpelevator:oauth');

		sender.postMessage('hello');
		sender.postMessage({ result: 'bogus' });
		expect(seen).toEqual([]);

		sender.postMessage({ result: 'connected', reason: 42, nonce: 7 }); // non-strings → null
		expect(seen).toEqual([{ result: 'connected', reason: null, nonce: null }]);

		stop();
		sender.postMessage({ result: 'error', reason: null, nonce: 'n' });
		expect(seen).toHaveLength(1); // unsubscribed — no further deliveries
	});
});

describe('openOauthPopup', () => {
	function fakePopup() {
		return {
			closed: false,
			opener: {} as unknown,
			sessionStorage: { setItem: vi.fn() }
		};
	}

	it('opens a uniquely named popup, disowns its opener, and marks it with the nonce', () => {
		const popup = fakePopup();
		const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);

		const handle = openOauthPopup();
		expect(handle?.popup).toBe(popup);
		expect(open).toHaveBeenCalledWith(
			'about:blank',
			`mcpelevator-oauth-${handle?.nonce}`,
			expect.stringContaining('popup=yes')
		);
		expect(popup.opener).toBeNull(); // reverse-tabnabbing guard
		expect(popup.sessionStorage.setItem).toHaveBeenCalledWith(MARKER, handle?.nonce);
	});

	it('issues a distinct nonce (and window name) per flow', () => {
		vi.spyOn(window, 'open')
			.mockReturnValueOnce(fakePopup() as unknown as Window)
			.mockReturnValueOnce(fakePopup() as unknown as Window);
		const first = openOauthPopup();
		const second = openOauthPopup();
		expect(first?.nonce).toBeTruthy();
		expect(second?.nonce).toBeTruthy();
		expect(first?.nonce).not.toBe(second?.nonce);
	});

	it('returns null when blocked', () => {
		vi.spyOn(window, 'open').mockReturnValue(null);
		expect(openOauthPopup()).toBeNull();
	});

	it('treats a throwing window.open as blocked (sandboxed/strict environments)', () => {
		vi.spyOn(window, 'open').mockImplementation(() => {
			throw new DOMException('blocked');
		});
		expect(openOauthPopup()).toBeNull();
	});
});

describe('popupCanRelay', () => {
	it('accepts a same-origin redirect_uri and rejects a cross-origin one', () => {
		const same = `https://as.example/auth?redirect_uri=${encodeURIComponent(
			`${ORIGIN}/api/oauth/callback`
		)}&state=s`;
		expect(popupCanRelay(same)).toBe(true);

		const cross = `https://as.example/auth?redirect_uri=${encodeURIComponent(
			'https://public.example.com/api/oauth/callback'
		)}&state=s`;
		expect(popupCanRelay(cross)).toBe(false);
	});

	it('errs toward the popup for missing or unparseable redirect_uri', () => {
		expect(popupCanRelay('https://as.example/auth?state=s')).toBe(true);
		expect(popupCanRelay('https://as.example/auth?redirect_uri=not-a-url')).toBe(true);
		expect(popupCanRelay('::not a url::')).toBe(true);
	});
});
