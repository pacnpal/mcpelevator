import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { completeOauthPopup, listenForOauthResult, openOauthPopup } from './oauthPopup';

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
	it('broadcasts the result, consumes the marker, and closes the popup', () => {
		sessionStorage.setItem(MARKER, '1');
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));

		completeOauthPopup(new URL(`${ORIGIN}/server/abc?oauth=connected`));

		expect(seen).toEqual([{ result: 'connected', reason: null }]);
		expect(close).toHaveBeenCalled();
		expect(sessionStorage.getItem(MARKER)).toBeNull(); // one-shot marker
		stop();
	});

	it('broadcasts the error result with its reason', () => {
		sessionStorage.setItem(MARKER, '1');
		vi.spyOn(window, 'close').mockImplementation(() => {});
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));

		completeOauthPopup(new URL(`${ORIGIN}/?oauth=error&reason=denied`));

		expect(seen).toEqual([{ result: 'error', reason: 'denied' }]);
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
		sessionStorage.setItem(MARKER, '1');
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});

		completeOauthPopup(new URL(`${ORIGIN}/server/abc`)); // no ?oauth=
		completeOauthPopup(new URL(`${ORIGIN}/server/abc?oauth=bogus`)); // unknown value

		expect(close).not.toHaveBeenCalled();
		expect(sessionStorage.getItem(MARKER)).toBe('1'); // marker not consumed
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

		sender.postMessage({ result: 'connected', reason: 42 }); // non-string reason → null
		expect(seen).toEqual([{ result: 'connected', reason: null }]);

		stop();
		sender.postMessage({ result: 'error', reason: null });
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

	it('opens a named popup, disowns its opener, and marks it', () => {
		const popup = fakePopup();
		const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);

		expect(openOauthPopup()).toBe(popup);
		expect(open).toHaveBeenCalledWith(
			'about:blank',
			'mcpelevator-oauth',
			expect.stringContaining('popup=yes')
		);
		expect(popup.opener).toBeNull(); // reverse-tabnabbing guard
		expect(popup.sessionStorage.setItem).toHaveBeenCalledWith(MARKER, '1');
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
