import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ServerTool, ToolCallResult } from '$lib/types';
import ToolRunner from './ToolRunner.svelte';

const api = vi.hoisted(() => ({
	callServerTool: vi.fn(),
	errorMessage: vi.fn((error: unknown) => (error instanceof Error ? error.message : 'error'))
}));

vi.mock('$lib/api', () => api);

const TOOL: ServerTool = {
	name: 'add',
	description: 'Add two integers.',
	input_schema: {
		type: 'object',
		properties: {
			a: { type: 'integer', description: 'first addend' },
			b: { type: 'integer' },
			label: { type: 'string' }
		},
		required: ['a', 'b']
	},
	has_output_schema: true
};

const RESULT: ToolCallResult = {
	is_error: false,
	content: [{ type: 'text', text: '5' }],
	structured_content: { result: 5 },
	duration_ms: 12
};

let dispose: (() => void) | undefined;

function render(tool: ServerTool = TOOL, runnable = true) {
	const target = document.createElement('div');
	document.body.append(target);
	const component = mount(ToolRunner, {
		target,
		props: { serverId: 'srv-1', tool, runnable }
	});
	flushSync();
	dispose = () => void unmount(component);
	return target;
}

function click(el: Element | null) {
	(el as HTMLButtonElement).click();
	flushSync();
}

function buttonByText(target: HTMLElement, text: string): HTMLButtonElement | null {
	return (
		Array.from(target.querySelectorAll('button')).find((b) =>
			(b.textContent ?? '').includes(text)
		) ?? null
	);
}

afterEach(() => {
	dispose?.();
	dispose = undefined;
	document.body.innerHTML = '';
	vi.clearAllMocks();
});

describe('ToolRunner', () => {
	it('is disabled when the server is not running', () => {
		const target = render(TOOL, false);
		const toggle = buttonByText(target, 'Try it');
		expect(toggle).not.toBeNull();
		expect(toggle!.disabled).toBe(true);
	});

	it('builds a form from the input schema and sends coerced arguments', async () => {
		api.callServerTool.mockResolvedValue(RESULT);
		const target = render();
		click(buttonByText(target, 'Try it'));

		const a = target.querySelector<HTMLInputElement>('#tr-add-a');
		const b = target.querySelector<HTMLInputElement>('#tr-add-b');
		expect(a).not.toBeNull();
		expect(b).not.toBeNull();
		// required markers are shown for schema-required fields
		expect(target.textContent).toContain('required');
		// the field description from the schema is surfaced
		expect(target.textContent).toContain('first addend');

		a!.value = '2';
		a!.dispatchEvent(new Event('input', { bubbles: true }));
		b!.value = '3';
		b!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		click(buttonByText(target, 'Run tool'));
		await vi.waitFor(() => expect(api.callServerTool).toHaveBeenCalledOnce());
		// numbers were coerced; the blank optional string was omitted
		expect(api.callServerTool).toHaveBeenCalledWith('srv-1', 'add', { a: 2, b: 3 });
		await vi.waitFor(() => expect(target.textContent).toContain('12 ms'));
		expect(target.textContent).toContain('"result": 5');
		expect(target.textContent).toContain('OK');
	});

	it('surfaces a tool error result distinctly', async () => {
		api.callServerTool.mockResolvedValue({
			is_error: true,
			content: [{ type: 'text', text: 'boom' }],
			structured_content: null,
			duration_ms: 3
		} satisfies ToolCallResult);
		const target = render();
		click(buttonByText(target, 'Try it'));
		const a = target.querySelector<HTMLInputElement>('#tr-add-a');
		a!.value = '1';
		a!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		click(buttonByText(target, 'Run tool'));
		await vi.waitFor(() => expect(target.textContent).toContain('TOOL ERROR'));
		expect(target.textContent).toContain('boom');
	});

	it('sends numeric enum selections as their original schema value, not text', async () => {
		api.callServerTool.mockResolvedValue(RESULT);
		const enumTool: ServerTool = {
			name: 'pick',
			description: '',
			input_schema: {
				type: 'object',
				properties: { level: { type: 'integer', enum: [1, 2, 3] } },
				required: ['level']
			}
		};
		const target = render(enumTool);
		click(buttonByText(target, 'Try it'));

		const select = target.querySelector<HTMLSelectElement>('#tr-pick-level');
		expect(select).not.toBeNull();
		select!.value = '2';
		select!.dispatchEvent(new Event('change', { bubbles: true }));
		flushSync();
		click(buttonByText(target, 'Run tool'));
		await vi.waitFor(() => expect(api.callServerTool).toHaveBeenCalledOnce());
		// the number 2 from the schema's enum — not the string "2"
		expect(api.callServerTool).toHaveBeenCalledWith('srv-1', 'pick', { level: 2 });
	});

	it('falls back to raw JSON for a schema-less tool and validates the object shape', async () => {
		api.callServerTool.mockResolvedValue(RESULT);
		const bare: ServerTool = { name: 'bare', description: '' };
		const target = render(bare);
		click(buttonByText(target, 'Try it'));

		const raw = target.querySelector<HTMLTextAreaElement>('#tr-bare-raw');
		expect(raw).not.toBeNull();
		raw!.value = '[1, 2]'; // not an object → client-side validation error
		raw!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		click(buttonByText(target, 'Run tool'));
		flushSync();
		expect(api.callServerTool).not.toHaveBeenCalled();
		expect(target.textContent).toContain('Arguments must be a JSON object.');

		raw!.value = '{"x": 1}';
		raw!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		click(buttonByText(target, 'Run tool'));
		await vi.waitFor(() => expect(api.callServerTool).toHaveBeenCalledOnce());
		expect(api.callServerTool).toHaveBeenCalledWith('srv-1', 'bare', { x: 1 });
	});
});
