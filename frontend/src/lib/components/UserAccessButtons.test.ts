import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { UserInfo } from '#lib/types.js';
import UserAccessButtons, { managedFields } from './UserAccessButtons.svelte';

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof UserAccessButtons>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(UserAccessButtons, { target, props });
	flushSync();
	dispose = () => void unmount(component);
	return target;
}

function button(target: HTMLElement, label: string): HTMLButtonElement {
	const el = [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
	if (!el) throw new Error(`no ${label} button`);
	return el;
}

function user(managed: UserInfo['managed']): UserInfo {
	return {
		id: 'u-1',
		name: 'ada',
		role: 'member',
		local_runners: false,
		control_mcp: true,
		servers_count: 0,
		tokens_count: 0,
		created_at: '2026-01-01T00:00:00Z',
		linked_issuer: null,
		managed
	};
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
});

describe('UserAccessButtons', () => {
	it('disables the fields the identity provider manages and says why', () => {
		const onpatch = vi.fn();
		const target = render({ user: user({ role: true, local_runners: false }), onpatch });
		const role = button(target, 'Make admin');
		expect(role.disabled).toBe(true);
		expect(role.title).toContain('oidc_admin_groups');
		const runners = button(target, 'Allow local runners');
		expect(runners.disabled).toBe(false);
		runners.click();
		expect(onpatch).toHaveBeenCalledWith({ local_runners: true });
	});

	it('locks local runners when that list manages it', () => {
		const target = render({
			user: user({ role: false, local_runners: true }),
			onpatch: vi.fn()
		});
		const runners = button(target, 'Allow local runners');
		expect(runners.disabled).toBe(true);
		expect(runners.title).toContain('oidc_local_runner_groups');
		expect(button(target, 'Make admin').disabled).toBe(false);
	});

	it('follows the SSO settings the page holds, so a save unlocks without a reload', () => {
		const sso = { oidc_enabled: true, oidc_admin_groups: ['admins'], oidc_local_runner_groups: [] };
		expect(managedFields(sso)).toEqual({ role: true, local_runners: false });
		expect(managedFields({ ...sso, oidc_admin_groups: [] })).toEqual({
			role: false,
			local_runners: false
		});
		expect(managedFields({ ...sso, oidc_local_runner_groups: ['builders'] })).toEqual({
			role: true,
			local_runners: true
		});
		expect(managedFields({ ...sso, oidc_enabled: false })).toEqual({
			role: false,
			local_runners: false
		});
		expect(managedFields(null)).toEqual({ role: false, local_runners: false });
	});
});
