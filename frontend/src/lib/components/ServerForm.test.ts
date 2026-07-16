import { flushSync, mount, unmount } from 'svelte';
import type { ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ServerForm from './ServerForm.svelte';

const api = vi.hoisted(() => ({
	getSettings: vi.fn().mockResolvedValue({ docker_runner: true })
}));

vi.mock('$lib/api', () => api);

let dispose: (() => void) | undefined;

function render(props: ComponentProps<typeof ServerForm>) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(ServerForm, { target, props });
	flushSync();
	dispose = () => void unmount(component);
	return target;
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('ServerForm setup script', () => {
	it('seeds and submits exact nonblank script text', () => {
		const onsubmit = vi.fn();
		const script = '\n  echo "keep spacing"  \n\nprintf done\n';
		const target = render({
			mode: 'edit',
			initial: {
				name: 'local',
				runner: 'command',
				command: '/usr/bin/server',
				args: [],
				setup_script: script
			},
			onsubmit
		});

		const textarea = target.querySelector<HTMLTextAreaElement>('#srv-setup-script');
		expect(textarea?.value).toBe(script);
		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);

		expect(onsubmit).toHaveBeenCalledWith(expect.objectContaining({ setup_script: script }));
	});

	it('keeps an unsupported script visible and blocks Docker until it is cleared', () => {
		const onsubmit = vi.fn();
		const target = render({
			initial: {
				name: 'image',
				runner: 'command',
				command: 'ghcr.io/example/server',
				args: [],
				setup_script: 'echo setup'
			},
			onsubmit
		});

		const docker = target.querySelector<HTMLInputElement>('input[name="runner"][value="docker"]');
		if (!docker) throw new Error('Docker runner input not found');
		docker.checked = true;
		docker.dispatchEvent(new Event('change', { bubbles: true }));
		flushSync();

		const textarea = target.querySelector<HTMLTextAreaElement>('#srv-setup-script');
		const submit = target.querySelector<HTMLButtonElement>('button[type="submit"]');
		expect(textarea?.value).toBe('echo setup');
		expect(textarea?.getAttribute('aria-invalid')).toBe('true');
		expect(target.textContent).toContain('Put setup in the image');
		expect(target.textContent).toContain('/bin/sh -e -c');
		expect(submit?.disabled).toBe(true);

		if (!textarea) throw new Error('Setup textarea not found');
		textarea.value = '   \n';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		expect(target.textContent).not.toContain('Put setup in the image');
		expect(submit?.disabled).toBe(false);

		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);
		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({ runner: 'docker', setup_script: '' })
		);
	});

	it('blocks a command runner that invokes the Docker CLI', () => {
		const target = render({
			initial: {
				name: 'docker command',
				runner: 'command',
				command: '/usr/bin/server',
				args: ['run', 'img:1'],
				setup_script: 'echo setup'
			},
			onsubmit: vi.fn()
		});

		const advanced = target.querySelector<HTMLButtonElement>('button[aria-expanded]');
		const command = target.querySelector<HTMLInputElement>('#srv-command');
		if (!advanced || !command) throw new Error('Command controls not found');
		advanced.click();
		flushSync();
		expect(advanced.getAttribute('aria-expanded')).toBe('false');
		command.value = '/usr/local/bin/docker';
		command.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		const textarea = target.querySelector<HTMLTextAreaElement>('#srv-setup-script');
		const submit = target.querySelector<HTMLButtonElement>('button[type="submit"]');
		expect(advanced.getAttribute('aria-expanded')).toBe('true');
		expect(textarea?.getAttribute('aria-invalid')).toBe('true');
		expect(target.textContent).toContain('Put setup in the image');
		expect(submit?.disabled).toBe(true);
	});
});
