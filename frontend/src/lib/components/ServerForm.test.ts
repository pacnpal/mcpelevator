import { flushSync, mount, unmount } from 'svelte';
import type { ComponentProps } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ServerForm from './ServerForm.svelte';

const api = vi.hoisted(() => ({
	getSettings: vi.fn().mockResolvedValue({ docker_runner: true }),
	previewOpenapi: vi.fn().mockResolvedValue({
		base_url: 'https://app.coolify.io/api/v1',
		title: 'Coolify',
		operations: 301,
		error: null
	}),
	// Full-permission principal so the form offers every runner (the multi-user
	// restriction path is exercised by the backend tests).
	getAuthStatus: vi.fn().mockResolvedValue({
		enforced: false,
		authenticated: false,
		user: { id: null, name: 'local operator', role: 'admin', local_runners: true }
	})
}));

vi.mock('#lib/api.js', () => api);

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

		const advanced = target.querySelector<HTMLButtonElement>('#srv-advanced-toggle');
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

describe('ServerForm docker run options', () => {
	it('seeds run options, previews them before the image, and submits run_args', () => {
		const onsubmit = vi.fn();
		const target = render({
			mode: 'edit',
			initial: {
				name: 'gh',
				runner: 'docker',
				command: 'img:1',
				args: ['serve'],
				run_args: ['--name', 'my-mcp'],
				env: { TOKEN: 'x' }
			},
			onsubmit
		});

		const runArgs = target.querySelector<HTMLTextAreaElement>('#srv-docker-run-args');
		expect(runArgs?.value).toBe('--name\nmy-mcp');
		// the live preview mirrors the real argv order: -e NAME, run options, --, image, args
		expect(target.textContent).toContain(
			'docker run -i --rm --init […] -e TOKEN --name my-mcp -- img:1 serve'
		);

		// editing the options updates the preview live
		if (!runArgs) throw new Error('Run options textarea not found');
		runArgs.value = '--shm-size=1g';
		runArgs.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		expect(target.textContent).toContain(
			'docker run -i --rm --init […] -e TOKEN --shm-size=1g -- img:1 serve'
		);

		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);
		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({ runner: 'docker', run_args: ['--shm-size=1g'] })
		);
	});

	it('sends empty run_args for non-docker runners', () => {
		const onsubmit = vi.fn();
		const target = render({
			initial: { name: 'local', runner: 'command', command: '/usr/bin/server', args: [] },
			onsubmit
		});
		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);
		expect(onsubmit).toHaveBeenCalledWith(expect.objectContaining({ run_args: [] }));
	});
});

describe('ServerForm source repository', () => {
	function submit(target: HTMLElement) {
		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);
	}

	it('seeds a repository install open and submits edited build steps', () => {
		const onsubmit = vi.fn();
		const target = render({
			initial: {
				name: 'console',
				runner: 'command',
				command: 'node',
				args: ['dist/mcp/server.js'],
				repo_url: 'https://github.com/ooples/mcp-console-automation',
				repo_ref: '',
				post_clone_script: 'npm ci'
			},
			onsubmit
		});

		const script = target.querySelector<HTMLTextAreaElement>('#srv-post-clone');
		const ref = target.querySelector<HTMLInputElement>('#srv-repo-ref');
		if (!script || !ref) throw new Error('Repository fields not found');
		expect(script.value).toBe('npm ci');
		script.value = 'npm ci\nnpm run build';
		script.dispatchEvent(new Event('input', { bubbles: true }));
		ref.value = ' v1.1.5 ';
		ref.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		submit(target);

		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({
				runner: 'command',
				command: 'node',
				repo_url: 'https://github.com/ooples/mcp-console-automation',
				repo_ref: 'v1.1.5',
				post_clone_script: 'npm ci\nnpm run build'
			})
		);
	});

	it('blocks a post-clone script without a repository', () => {
		const target = render({
			initial: {
				name: 'x',
				runner: 'command',
				command: 'node',
				args: [],
				repo_url: '',
				post_clone_script: 'npm ci'
			},
			onsubmit: vi.fn()
		});
		const submitButton = target.querySelector<HTMLButtonElement>('button[type="submit"]');
		expect(target.textContent).toContain('A post-clone script needs a repository URL.');
		expect(submitButton?.disabled).toBe(true);
	});

	it('sends blank repository fields when there is none', () => {
		const onsubmit = vi.fn();
		const target = render({
			initial: { name: 'plain', runner: 'command', command: '/usr/bin/server', args: [] },
			onsubmit
		});
		submit(target);
		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({ repo_url: '', repo_ref: '', post_clone_script: '' })
		);
	});
});

describe('ServerForm health check', () => {
	function submit(target: HTMLElement) {
		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);
	}

	function type(el: HTMLInputElement | HTMLTextAreaElement | null, value: string) {
		if (!el) throw new Error('field not found');
		el.value = value;
		el.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
	}

	it('sends no check by default', () => {
		const onsubmit = vi.fn();
		const target = render({
			initial: { name: 'plain', runner: 'command', command: '/usr/bin/server', args: [] },
			onsubmit
		});
		submit(target);
		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({
				health_check_tool: null,
				health_check_arguments: null,
				health_check_command: null,
				health_check_interval_s: null
			})
		);
	});

	it('seeds a tool check and submits its arguments and interval', () => {
		const onsubmit = vi.fn();
		const target = render({
			mode: 'edit',
			initial: {
				name: 'browser',
				runner: 'remote',
				command: 'https://example.com/mcp',
				args: ['streamable-http'],
				health_check_tool: 'navigate',
				health_check_arguments: { url: 'about:blank' },
				health_check_interval_s: 300
			},
			onsubmit
		});
		expect(target.querySelector<HTMLSelectElement>('#srv-health-mode')?.value).toBe('tool');
		// A remote server has no host shell: the command form isn't offered.
		expect(target.querySelector('#srv-health-mode option[value="command"]')).toBeNull();
		expect(target.querySelector<HTMLInputElement>('#srv-health-tool')?.value).toBe('navigate');
		submit(target);
		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({
				health_check_tool: 'navigate',
				health_check_arguments: { url: 'about:blank' },
				health_check_command: null,
				health_check_interval_s: 300
			})
		);
	});

	it('blocks invalid arguments and a too-short interval', () => {
		const target = render({
			initial: {
				name: 'x',
				runner: 'command',
				command: '/usr/bin/server',
				args: [],
				health_check_tool: 'ping'
			},
			onsubmit: vi.fn()
		});
		const submitButton = target.querySelector<HTMLButtonElement>('button[type="submit"]');
		expect(submitButton?.disabled).toBe(false);
		type(target.querySelector('#srv-health-args'), '[1, 2]');
		expect(submitButton?.disabled).toBe(true);
		type(target.querySelector('#srv-health-args'), '{"a": 1}');
		expect(submitButton?.disabled).toBe(false);
		type(target.querySelector('#srv-health-interval'), '5');
		expect(submitButton?.disabled).toBe(true);
	});

	it('submits a shell command check for a local runner', () => {
		const onsubmit = vi.fn();
		const target = render({
			initial: {
				name: 'local',
				runner: 'command',
				command: '/usr/bin/server',
				args: [],
				health_check_command: 'curl -fsS http://127.0.0.1:3000/health'
			},
			onsubmit
		});
		expect(target.querySelector<HTMLSelectElement>('#srv-health-mode')?.value).toBe('command');
		submit(target);
		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({
				health_check_tool: null,
				health_check_command: 'curl -fsS http://127.0.0.1:3000/health',
				health_check_interval_s: null
			})
		);
	});
});

describe('ServerForm secret env', () => {
	it('keeps a hidden secret blank and sends it back as hidden', () => {
		const onsubmit = vi.fn();
		const target = render({
			mode: 'edit',
			initial: {
				name: 'api',
				runner: 'command',
				command: '/usr/bin/server',
				args: [],
				env: { API_KEY: '<hidden>', REGION: 'eu' },
				secret_env: ['API_KEY']
			},
			onsubmit
		});

		const values = target.querySelectorAll<HTMLInputElement>(
			'input[aria-label="Environment variable value"]'
		);
		expect(values[0].type).toBe('password');
		expect(values[0].value).toBe('');
		expect(values[1].type).toBe('text');
		target.querySelector('form')?.dispatchEvent(
			new SubmitEvent('submit', { bubbles: true, cancelable: true })
		);

		expect(onsubmit).toHaveBeenCalledWith(
			expect.objectContaining({
				env: { API_KEY: '<hidden>', REGION: 'eu' },
				secret_env: ['API_KEY']
			})
		);
	});
});

describe('ServerForm OpenAPI base URL preview', () => {
	it("shows the document's own base URL while the override is blank", async () => {
		vi.useFakeTimers();
		try {
			const target = render({
				mode: 'create',
				initial: {
					name: 'coolify',
					runner: 'openapi',
					command: 'https://example.com/openapi.yaml',
					args: []
				},
				onsubmit: vi.fn()
			});
			await vi.advanceTimersByTimeAsync(700);
			flushSync();
			expect(api.previewOpenapi).toHaveBeenCalledWith('https://example.com/openapi.yaml', '');
			expect(target.textContent).toContain('https://app.coolify.io/api/v1');
		} finally {
			vi.useRealTimers();
		}
	});
});
