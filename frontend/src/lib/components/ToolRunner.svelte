<script lang="ts">
	import { callServerTool, errorMessage } from '$lib/api';
	import type { ServerTool, ToolCallResult } from '$lib/types';

	let {
		serverId,
		tool,
		runnable
	}: {
		serverId: string;
		tool: ServerTool;
		/** The server is running, so calls can actually be made. */
		runnable: boolean;
	} = $props();

	// ---- Schema → form fields -------------------------------------------------
	//
	// Simple property types (string/number/integer/boolean, enums) become native
	// inputs; anything else (objects, arrays, unions, schema-less) falls back to a
	// JSON textarea per field. A raw-JSON mode edits the whole argument object.

	type JsonSchema = Record<string, unknown>;

	interface Field {
		name: string;
		schema: JsonSchema;
		required: boolean;
		kind: 'string' | 'number' | 'boolean' | 'enum' | 'json';
		/** Original enum values from the schema (may be numbers/booleans) — the
		 * select edits their String() form and submit maps back to the original,
		 * so a numeric enum isn't sent as text. */
		enumValues: unknown[];
		description: string;
	}

	function fieldKind(schema: JsonSchema): Field['kind'] {
		if (Array.isArray(schema.enum)) return 'enum';
		const type = schema.type;
		if (type === 'string') return 'string';
		if (type === 'number' || type === 'integer') return 'number';
		if (type === 'boolean') return 'boolean';
		return 'json';
	}

	const fields = $derived.by((): Field[] => {
		const schema = (tool.input_schema ?? {}) as JsonSchema;
		const props = (schema.properties ?? {}) as Record<string, JsonSchema>;
		const required = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
		return Object.entries(props).map(([name, propSchema]) => ({
			name,
			schema: propSchema,
			required: required.has(name),
			kind: fieldKind(propSchema),
			enumValues: Array.isArray(propSchema.enum) ? (propSchema.enum as unknown[]) : [],
			description: typeof propSchema.description === 'string' ? propSchema.description : ''
		}));
	});

	// Whether the schema declares any properties at all. A schema-less tool (or one
	// with additionalProperties) is still callable via raw-JSON mode.
	const hasFields = $derived(fields.length > 0);

	let open = $state(false);
	let rawMode = $state(false);
	let rawText = $state('{}');
	// Field values keyed by property name. Mostly text; a number-typed input binds
	// a number. Everything is normalized through String() at submit time.
	let values = $state<Record<string, string | number>>({});
	let checks = $state<Record<string, boolean>>({});

	let busy = $state(false);
	let formError = $state<string | null>(null);
	let result = $state<ToolCallResult | null>(null);
	let callError = $state<string | null>(null);

	function buildArguments(): Record<string, unknown> {
		if (rawMode || !hasFields) {
			const text = rawText.trim();
			if (!text) return {};
			const parsed = JSON.parse(text);
			if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
				throw new Error('Arguments must be a JSON object.');
			}
			return parsed as Record<string, unknown>;
		}
		const out: Record<string, unknown> = {};
		for (const field of fields) {
			if (field.kind === 'boolean') {
				// Only send a boolean the operator actually touched or that's required —
				// an omitted optional flag keeps the tool's own default.
				if (field.name in checks) out[field.name] = checks[field.name] ?? false;
				else if (field.required) out[field.name] = false;
				continue;
			}
			const raw = String(values[field.name] ?? '').trim();
			if (!raw) continue; // blank optional field → let the tool default it
			if (field.kind === 'enum') {
				// The select's value is the option's INDEX into the schema's enum, so
				// the original value (number, boolean, string) round-trips exactly —
				// String() as identity would collide distinct values like [1, "1"].
				const idx = Number(raw);
				out[field.name] =
					Number.isInteger(idx) && idx >= 0 && idx < field.enumValues.length
						? field.enumValues[idx]
						: raw;
			} else if (field.kind === 'number') {
				const n = Number(raw);
				if (Number.isNaN(n)) throw new Error(`"${field.name}" must be a number.`);
				out[field.name] = n;
			} else if (field.kind === 'json') {
				try {
					out[field.name] = JSON.parse(raw);
				} catch {
					throw new Error(`"${field.name}" must be valid JSON.`);
				}
			} else {
				out[field.name] = raw;
			}
		}
		return out;
	}

	async function run() {
		if (busy || !runnable) return;
		formError = null;
		callError = null;
		let args: Record<string, unknown>;
		try {
			args = buildArguments();
		} catch (err) {
			formError = err instanceof Error ? err.message : 'Invalid arguments.';
			return;
		}
		busy = true;
		try {
			result = await callServerTool(serverId, tool.name, args);
		} catch (err) {
			result = null;
			callError = errorMessage(err);
		} finally {
			busy = false;
		}
	}

	// Text blocks render as plain text; everything else falls back to JSON.
	function textOf(block: Record<string, unknown>): string | null {
		return block.type === 'text' && typeof block.text === 'string' ? block.text : null;
	}

	const resultJson = $derived(
		result?.structured_content ? JSON.stringify(result.structured_content, null, 2) : null
	);
</script>

<div class="flex flex-col gap-2">
	<button
		type="button"
		onclick={() => (open = !open)}
		disabled={!runnable}
		aria-expanded={open}
		title={runnable ? `Try ${tool.name}` : 'Start the server to try its tools'}
		class="inline-flex w-fit items-center gap-1 rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2 py-1 text-[11px] font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-not-allowed disabled:opacity-50"
	>
		<svg class="size-3" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
			<path d="M8 5v14l11-7z" />
		</svg>
		{open ? 'Hide' : 'Try it'}
	</button>

	{#if open}
		<div
			class="flex flex-col gap-3 rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-3"
		>
			{#if hasFields && !rawMode}
				<div class="flex flex-col gap-2.5">
					{#each fields as field (field.name)}
						<div class="flex flex-col gap-1">
							<label
								for={`tr-${tool.name}-${field.name}`}
								class="flex items-baseline gap-1.5 font-mono text-xs text-[var(--color-ink)]"
							>
								{field.name}
								{#if field.required}
									<span class="text-[10px] text-[var(--color-state-unhealthy)]">required</span>
								{/if}
								{#if field.kind === 'json'}
									<span class="text-[10px] text-[var(--color-ink-dim)]">JSON</span>
								{/if}
							</label>
							{#if field.description}
								<p class="text-[11px] leading-snug text-[var(--color-ink-dim)]">
									{field.description}
								</p>
							{/if}
							{#if field.kind === 'boolean'}
								<label class="flex w-fit items-center gap-2 text-xs text-[var(--color-ink-muted)]">
									<input
										id={`tr-${tool.name}-${field.name}`}
										type="checkbox"
										checked={checks[field.name] ?? false}
										onchange={(e) => (checks[field.name] = (e.currentTarget as HTMLInputElement).checked)}
									/>
									{checks[field.name] ? 'true' : 'false'}
								</label>
							{:else if field.kind === 'enum'}
								<select
									id={`tr-${tool.name}-${field.name}`}
									bind:value={values[field.name]}
									class="w-full cursor-pointer rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
								>
									<option value="">(unset)</option>
									{#each field.enumValues as v, i (i)}
										<option value={String(i)}>{String(v)}</option>
									{/each}
								</select>
							{:else if field.kind === 'json'}
								<textarea
									id={`tr-${tool.name}-${field.name}`}
									bind:value={values[field.name]}
									rows="2"
									spellcheck="false"
									placeholder={'{ … }'}
									class="resize-y rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
								></textarea>
							{:else}
								<input
									id={`tr-${tool.name}-${field.name}`}
									type={field.kind === 'number' ? 'number' : 'text'}
									step="any"
									bind:value={values[field.name]}
									autocomplete="off"
									spellcheck="false"
									class="rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
								/>
							{/if}
						</div>
					{/each}
				</div>
			{:else}
				<div class="flex flex-col gap-1">
					<label
						for={`tr-${tool.name}-raw`}
						class="font-mono text-xs text-[var(--color-ink)]"
					>
						arguments <span class="text-[10px] text-[var(--color-ink-dim)]">JSON object</span>
					</label>
					<textarea
						id={`tr-${tool.name}-raw`}
						bind:value={rawText}
						rows="4"
						spellcheck="false"
						class="resize-y rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
					></textarea>
				</div>
			{/if}

			<div class="flex flex-wrap items-center gap-2">
				<button
					type="button"
					onclick={run}
					disabled={busy || !runnable}
					aria-busy={busy}
					class="inline-flex items-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px disabled:cursor-wait disabled:opacity-60"
				>
					{#if busy}
						<svg class="size-3.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						Running…
					{:else}
						Run tool
					{/if}
				</button>
				{#if hasFields}
					<button
						type="button"
						onclick={() => (rawMode = !rawMode)}
						class="rounded-md border border-[var(--color-line)] px-2.5 py-1.5 text-[11px] font-medium text-[var(--color-ink-dim)] transition hover:text-[var(--color-ink)]"
					>
						{rawMode ? 'Form fields' : 'Raw JSON'}
					</button>
				{/if}
				{#if result}
					<span class="font-mono text-[11px] text-[var(--color-ink-dim)]">
						{result.duration_ms} ms
					</span>
				{/if}
			</div>

			{#if formError}
				<p class="text-xs text-[var(--color-state-failed)]" role="alert">{formError}</p>
			{/if}
			{#if callError}
				<p
					class="rounded-md border px-2.5 py-2 font-mono text-xs leading-relaxed"
					style="border-color: color-mix(in oklab, var(--color-state-failed) 30%, transparent); color: var(--color-state-failed);"
					role="alert"
				>
					{callError}
				</p>
			{/if}

			{#if result}
				<div class="flex flex-col gap-1.5">
					<span
						class="w-fit rounded-md border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide"
						style={result.is_error
							? 'border-color: color-mix(in oklab, var(--color-state-failed) 40%, transparent); color: var(--color-state-failed);'
							: 'border-color: color-mix(in oklab, var(--color-accent) 40%, transparent); color: var(--color-accent);'}
					>
						{result.is_error ? 'TOOL ERROR' : 'OK'}
					</span>
					{#if resultJson}
						<pre
							class="m-0 max-h-72 overflow-auto rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-[var(--color-ink)]">{resultJson}</pre>
					{/if}
					{#each result.content as block, i (i)}
						{#if textOf(block) !== null}
							{#if !resultJson || result.is_error}
								<pre
									class="m-0 max-h-72 overflow-auto rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-[var(--color-ink)]">{textOf(block)}</pre>
							{/if}
						{:else}
							<pre
								class="m-0 max-h-72 overflow-auto rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-[var(--color-ink-muted)]">{JSON.stringify(block, null, 2)}</pre>
						{/if}
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>
