// jsdom's localStorage is unreliable under an opaque origin, so install a small
// deterministic in-memory implementation for tests of storage-backed code.
class MemoryStorage implements Storage {
	private store = new Map<string, string>();
	get length(): number {
		return this.store.size;
	}
	clear(): void {
		this.store.clear();
	}
	getItem(key: string): string | null {
		return this.store.has(key) ? (this.store.get(key) as string) : null;
	}
	setItem(key: string, value: string): void {
		this.store.set(key, String(value));
	}
	removeItem(key: string): void {
		this.store.delete(key);
	}
	key(index: number): string | null {
		return Array.from(this.store.keys())[index] ?? null;
	}
}

Object.defineProperty(globalThis, 'localStorage', {
	value: new MemoryStorage(),
	writable: true,
	configurable: true
});

// jsdom implements neither matchMedia nor ResizeObserver, and LayerChart needs
// both: svelte/motion consults `prefers-reduced-motion` at import time, and every
// chart measures its container to size the plot. Minimal stubs — enough for the
// components to mount and render marks under test.
if (typeof window !== 'undefined') {
	if (!window.matchMedia) {
		window.matchMedia = ((query: string) => ({
			matches: false,
			media: query,
			onchange: null,
			addEventListener: () => {},
			removeEventListener: () => {},
			addListener: () => {},
			removeListener: () => {},
			dispatchEvent: () => false
		})) as typeof window.matchMedia;
	}
	// jsdom renders <dialog> as an element but implements none of its modal behavior —
	// no showModal/show/close, and no close event — so a component that opens a real
	// modal dialog can't even mount under it. Minimal stand-in: track `open` and let
	// close() fire the event components listen for. The focus trap and inert background
	// are the browser's to provide; nothing here pretends to test those.
	const dialogProto = window.HTMLDialogElement?.prototype;
	if (dialogProto && !dialogProto.showModal) {
		const open = function (this: HTMLDialogElement) {
			this.open = true;
		};
		dialogProto.showModal = open;
		dialogProto.show = open;
		dialogProto.close = function (this: HTMLDialogElement, returnValue?: string) {
			if (!this.open) return;
			this.open = false;
			if (returnValue !== undefined) this.returnValue = returnValue;
			this.dispatchEvent(new Event('close'));
		};
	}

	if (!window.ResizeObserver) {
		window.ResizeObserver = class {
			observe(): void {}
			unobserve(): void {}
			disconnect(): void {}
		} as unknown as typeof window.ResizeObserver;
		globalThis.ResizeObserver = window.ResizeObserver;
	}
}
