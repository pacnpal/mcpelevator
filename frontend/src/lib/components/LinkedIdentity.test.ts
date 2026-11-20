import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import LinkedIdentity from './LinkedIdentity.svelte';

const api = vi.hoisted(() => ({
	unlinkUserIdentity: vi.fn(),
	errorMessage: vi.fn((error: unknown) => (error instanceof Error ? error.message : 'error'))
}));
const toast = vi.hoisted(() => ({ flashToast: vi.fn() }));

vi.mock('#lib/api.js', () => api);
vi.mock('#lib/toast.svelte.js', () => toast);

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof LinkedIdentity>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(LinkedIdentity, { target, props });
	flushSync();
	dispose = () => void unmount(component);
	return target;
}

function button(target: HTMLElement, label: string): HTMLButtonElement {
	const el = [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
	if (!el) throw new Error(`no ${label} button`);
	return el;
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('LinkedIdentity', () => {
	it('shows the issuer host and unlinks only after the confirm', async () => {
		api.unlinkUserIdentity.mockResolvedValue(undefined);
		const onunlinked = vi.fn();
		const target = render({
			userId: 'u-1',
			issuer: 'https://login.example.com/realms/acme',
			onunlinked
		});
		expect(target.textContent).toContain('linked · login.example.com');

		button(target, 'Unlink').click();
		flushSync();
		expect(api.unlinkUserIdentity).not.toHaveBeenCalled();

		button(target, 'Confirm unlink').click();
		await vi.waitFor(() => expect(onunlinked).toHaveBeenCalled());
		expect(api.unlinkUserIdentity).toHaveBeenCalledWith('u-1');
	});

	it('the checkbox sends block=true', async () => {
		api.unlinkUserIdentity.mockResolvedValue(undefined);
		const onunlinked = vi.fn();
		const target = render({ userId: 'u-1', issuer: 'https://idp.example.com', onunlinked });
		button(target, 'Unlink').click();
		flushSync();
		const box = target.querySelector<HTMLInputElement>('input[type="checkbox"]');
		if (!box) throw new Error('no checkbox');
		expect(box.checked).toBe(false);
		expect(box.closest('label')?.textContent).toContain("Don't let this identity match this user again");
		box.click();
		flushSync();
		button(target, 'Confirm unlink').click();
		await vi.waitFor(() => expect(onunlinked).toHaveBeenCalledWith(true));
		expect(api.unlinkUserIdentity).toHaveBeenCalledWith('u-1', true);
	});

	it('cancel leaves the link alone', () => {
		const target = render({ userId: 'u-1', issuer: 'https://idp.example.com', onunlinked: vi.fn() });
		button(target, 'Unlink').click();
		flushSync();
		button(target, 'Cancel').click();
		flushSync();
		expect(api.unlinkUserIdentity).not.toHaveBeenCalled();
		expect(button(target, 'Unlink')).toBeTruthy();
	});

	it('a failed unlink is toasted', async () => {
		api.unlinkUserIdentity.mockRejectedValue(new Error('user not found'));
		const onunlinked = vi.fn();
		const target = render({ userId: 'u-1', issuer: 'https://idp.example.com', onunlinked });
		button(target, 'Unlink').click();
		flushSync();
		button(target, 'Confirm unlink').click();
		await vi.waitFor(() => expect(toast.flashToast).toHaveBeenCalledWith('user not found'));
		expect(onunlinked).not.toHaveBeenCalled();
	});
});
