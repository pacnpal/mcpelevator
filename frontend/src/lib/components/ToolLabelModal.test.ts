import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ToolOverride } from '$lib/types';
import ToolLabelModal from './ToolLabelModal.svelte';

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof ToolLabelModal>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(ToolLabelModal, { target, props });
	flushSync();
	dispose = () => void unmount(component);
	return target;
}

function field(target: HTMLElement, label: string): HTMLInputElement | HTMLTextAreaElement {
	const wrapper = [...target.querySelectorAll('label')].find((el) =>
		el.textContent?.trim().startsWith(label)
	);
	const el = wrapper?.querySelector('input, textarea');
	if (!(el instanceof HTMLInputElement) && !(el instanceof HTMLTextAreaElement)) {
		throw new Error(`${label} field not found`);
	}
	return el;
}

function button(target: HTMLElement, label: string): HTMLButtonElement {
	const found = [...target.querySelectorAll('button')].find(
		(el) => el.textContent?.trim() === label
	);
	if (!(found instanceof HTMLButtonElement)) throw new Error(`${label} button not found`);
	return found;
}

function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
	el.value = value;
	el.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('ToolLabelModal', () => {
	it('opens on the staged override and saves a trimmed one', () => {
		const onsave = vi.fn();
		const onclose = vi.fn();
		const target = render({
			upstreamName: 'search_repos',
			override: { name: 'search' } satisfies ToolOverride,
			servedDescription: 'Search all the repositories.',
			onsave,
			onclose
		});

		expect(field(target, 'Name').value).toBe('search');
		type(field(target, 'Description'), '  Find repos.  ');
		button(target, 'Save').click();

		expect(onsave).toHaveBeenCalledWith({ name: 'search', description: 'Find repos.' });
		expect(onclose).toHaveBeenCalled();
	});

	it('drops a cleared field so the upstream label is restored', () => {
		const onsave = vi.fn();
		const target = render({
			upstreamName: 'search_repos',
			override: { name: 'search', description: 'Old text.' },
			onsave
		});

		type(field(target, 'Name'), '');
		type(field(target, 'Description'), '   ');
		button(target, 'Save').click();

		// An override with nothing left means "keep the upstream's" for both fields.
		expect(onsave).toHaveBeenCalledWith({});
	});

	it('discards the draft on cancel', () => {
		const onsave = vi.fn();
		const onclose = vi.fn();
		const target = render({ upstreamName: 'tool', override: {}, onsave, onclose });

		type(field(target, 'Name'), 'renamed');
		button(target, 'Cancel').click();

		expect(onsave).not.toHaveBeenCalled();
		expect(onclose).toHaveBeenCalled();
	});

	it('warns when the rename lands on a name another exposed tool holds', () => {
		const target = render({
			upstreamName: 'tool_a',
			override: {},
			takenNames: new Set(['tool_b'])
		});

		expect(target.textContent).not.toContain('will refuse this rename');
		type(field(target, 'Name'), 'tool_b');
		expect(target.textContent).toContain('will refuse this rename');

		// The tool's own upstream name is never a collision with itself.
		type(field(target, 'Name'), 'tool_a');
		expect(target.textContent).not.toContain('will refuse this rename');
	});

	it("offers the served description as the placeholder, unless it is the text being cleared", () => {
		const target = render({
			upstreamName: 'tool',
			override: {},
			servedDescription: 'What the client sees now.'
		});
		expect(field(target, 'Description').placeholder).toBe('What the client sees now.');

		dispose?.();
		document.body.innerHTML = '';
		const restoring = render({
			upstreamName: 'tool',
			override: {},
			servedDescription: 'The override being removed.',
			restoringDescription: true
		});
		expect(field(restoring, 'Description').placeholder).toBe("The upstream's description");
	});

	it('opens as a real modal and reports every close through one path', () => {
		const onclose = vi.fn();
		const target = render({ upstreamName: 'tool', override: {}, onclose });
		const dialog = target.querySelector('dialog');
		if (!(dialog instanceof HTMLDialogElement)) throw new Error('no dialog rendered');

		// showModal (not show) is what gives the focus trap, the inert background, and
		// Escape — the reason this is a native dialog rather than a positioned div.
		expect(dialog.open).toBe(true);

		// Escape, the close button, Cancel, and a backdrop click all end at the dialog's
		// own close event, so the caller has exactly one signal to handle.
		dialog.close();
		expect(onclose).toHaveBeenCalledTimes(1);
	});
});
