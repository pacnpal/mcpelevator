import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import UserActivation from './UserActivation.svelte';

const api = vi.hoisted(() => ({
	updateUser: vi.fn(),
	errorMessage: vi.fn((error: unknown) => (error instanceof Error ? error.message : 'error'))
}));
const toast = vi.hoisted(() => ({ flashToast: vi.fn() }));

vi.mock('#lib/api.js', () => api);
vi.mock('#lib/toast.svelte.js', () => toast);

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof UserActivation>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(UserActivation, { target, props });
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

describe('UserActivation', () => {
	it('deactivates an active user only after the confirm', async () => {
		const updated = { id: 'u-1', active: false };
		api.updateUser.mockResolvedValue(updated);
		const onchanged = vi.fn();
		const target = render({ userId: 'u-1', active: true, onchanged });
		expect(target.textContent).not.toContain('deactivated');

		button(target, 'Deactivate').click();
		flushSync();
		expect(api.updateUser).not.toHaveBeenCalled();

		button(target, 'Confirm deactivate').click();
		await vi.waitFor(() => expect(onchanged).toHaveBeenCalledWith(updated));
		expect(api.updateUser).toHaveBeenCalledWith('u-1', { active: false });
	});

	it('shows the badge and reactivates after the confirm', async () => {
		api.updateUser.mockResolvedValue({ id: 'u-1', active: true });
		const onchanged = vi.fn();
		const target = render({ userId: 'u-1', active: false, onchanged });
		expect(target.textContent).toContain('deactivated');

		button(target, 'Reactivate').click();
		flushSync();
		expect(api.updateUser).not.toHaveBeenCalled();
		button(target, 'Confirm reactivate').click();
		await vi.waitFor(() => expect(onchanged).toHaveBeenCalled());
		expect(api.updateUser).toHaveBeenCalledWith('u-1', { active: true });
	});

	it('cancel changes nothing', () => {
		const target = render({ userId: 'u-1', active: true, onchanged: vi.fn() });
		button(target, 'Deactivate').click();
		flushSync();
		button(target, 'Cancel').click();
		flushSync();
		expect(api.updateUser).not.toHaveBeenCalled();
		expect(button(target, 'Deactivate')).toBeTruthy();
	});

	it('a refusal is toasted', async () => {
		api.updateUser.mockRejectedValue(new Error('cannot deactivate the last admin'));
		const onchanged = vi.fn();
		const target = render({ userId: 'u-1', active: true, onchanged });
		button(target, 'Deactivate').click();
		flushSync();
		button(target, 'Confirm deactivate').click();
		await vi.waitFor(() =>
			expect(toast.flashToast).toHaveBeenCalledWith('cannot deactivate the last admin')
		);
		expect(onchanged).not.toHaveBeenCalled();
	});
});
