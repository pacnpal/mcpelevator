import { afterEach, describe, expect, it, vi } from 'vitest';

import { forwardOauthResultToOpener, listenForOauthResult, openOauthPopup } from './oauthPopup';

const ORIGIN = window.location.origin;

function fakeOpener() {
	return { closed: false, postMessage: vi.fn() };
}

afterEach(() => {
	vi.restoreAllMocks();
	Object.defineProperty(window, 'opener', { value: null, writable: true, configurable: true });
});

describe('forwardOauthResultToOpener', () => {
	it('posts the result to a same-origin opener and closes the popup', () => {
		const opener = fakeOpener();
		Object.defineProperty(window, 'opener', { value: opener, configurable: true });
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});

		forwardOauthResultToOpener(new URL(`${ORIGIN}/server/abc?oauth=connected`));

		expect(opener.postMessage).toHaveBeenCalledWith(
			{ type: 'mcpelevator:oauth', result: 'connected', reason: null },
			ORIGIN
		);
		expect(close).toHaveBeenCalled();
	});

	it('forwards the error result with its reason', () => {
		const opener = fakeOpener();
		Object.defineProperty(window, 'opener', { value: opener, configurable: true });
		vi.spyOn(window, 'close').mockImplementation(() => {});

		forwardOauthResultToOpener(new URL(`${ORIGIN}/?oauth=error&reason=denied`));

		expect(opener.postMessage).toHaveBeenCalledWith(
			{ type: 'mcpelevator:oauth', result: 'error', reason: 'denied' },
			ORIGIN
		);
	});

	it('does nothing without an oauth result or without an opener', () => {
		const opener = fakeOpener();
		Object.defineProperty(window, 'opener', { value: opener, configurable: true });
		const close = vi.spyOn(window, 'close').mockImplementation(() => {});

		forwardOauthResultToOpener(new URL(`${ORIGIN}/server/abc`)); // no ?oauth=
		forwardOauthResultToOpener(new URL(`${ORIGIN}/server/abc?oauth=bogus`)); // unknown value
		expect(opener.postMessage).not.toHaveBeenCalled();

		Object.defineProperty(window, 'opener', { value: null, configurable: true });
		forwardOauthResultToOpener(new URL(`${ORIGIN}/server/abc?oauth=connected`));
		expect(close).not.toHaveBeenCalled();
	});
});

describe('listenForOauthResult', () => {
	it('delivers same-origin oauth messages and ignores everything else', () => {
		const seen: unknown[] = [];
		const stop = listenForOauthResult((result) => seen.push(result));

		// Wrong origin — ignored.
		window.dispatchEvent(
			new MessageEvent('message', {
				origin: 'https://evil.example',
				data: { type: 'mcpelevator:oauth', result: 'connected', reason: null }
			})
		);
		// Unrelated message shapes — ignored.
		window.dispatchEvent(new MessageEvent('message', { origin: ORIGIN, data: 'hello' }));
		window.dispatchEvent(
			new MessageEvent('message', { origin: ORIGIN, data: { type: 'other', result: 'connected' } })
		);
		window.dispatchEvent(
			new MessageEvent('message', {
				origin: ORIGIN,
				data: { type: 'mcpelevator:oauth', result: 'bogus' }
			})
		);
		expect(seen).toEqual([]);

		window.dispatchEvent(
			new MessageEvent('message', {
				origin: ORIGIN,
				data: { type: 'mcpelevator:oauth', result: 'error', reason: 'denied' }
			})
		);
		expect(seen).toEqual([{ result: 'error', reason: 'denied' }]);

		stop();
		window.dispatchEvent(
			new MessageEvent('message', {
				origin: ORIGIN,
				data: { type: 'mcpelevator:oauth', result: 'connected', reason: null }
			})
		);
		expect(seen).toHaveLength(1); // unsubscribed — no further deliveries
	});
});

describe('openOauthPopup', () => {
	it('opens a named popup and returns null when blocked', () => {
		const open = vi.spyOn(window, 'open').mockReturnValue(null);
		expect(openOauthPopup()).toBeNull();
		expect(open).toHaveBeenCalledWith(
			'about:blank',
			'mcpelevator-oauth',
			expect.stringContaining('popup=yes')
		);
	});

	it('treats a throwing window.open as blocked (sandboxed/strict environments)', () => {
		vi.spyOn(window, 'open').mockImplementation(() => {
			throw new DOMException('blocked');
		});
		expect(openOauthPopup()).toBeNull();
	});
});
