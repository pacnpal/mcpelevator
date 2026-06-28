<script lang="ts">
	import { untrack } from 'svelte';
	import RunnerBadge from './RunnerBadge.svelte';
	import { REMOTE_TRANSPORTS, canonicalRemoteTransport } from '$lib/remote';
	import type { Runner, ServerAuthProvider, ServerCreate } from '$lib/types';

	type Mode = 'create' | 'edit';

	type EnvRow = { id: number; key: string; value: string };

	// Per-server auth options. `inherit` defers to the global default set on
	// the Settings page.
	const AUTH_OPTIONS: { value: ServerAuthProvider; label: string }[] = [
		{ value: 'inherit', label: 'inherit' },
		{ value: 'none', label: 'none' },
		{ value: 'bearer', label: 'bearer' }
	];

	let {
		mode = 'create',
		initial,
		busy = false,
		error = null,
		submitLabel,
		onsubmit,
		oncancel
	}: {
		mode?: Mode;
		/** Prefill values (edit) or partial defaults (create). `slug` is only used
		 * in edit mode, where it's surfaced as an editable identity field. */
		initial?: (Partial<ServerCreate> & { slug?: string }) | null;
		busy?: boolean;
		/** API error text to surface inline, if any. */
		error?: string | null;
		submitLabel?: string;
		/** Receives a fully-built ServerCreate-shaped payload. In edit mode it also
		 * carries `slug` when the operator renamed it. */
		onsubmit: (payload: ServerCreate & { slug?: string }) => void;
		oncancel?: () => void;
	} = $props();

	// npx / uvx / command / remote are offered in the friendly UI. `docker` is a
	// valid backend runner but out of scope for this form; an imported docker
	// server can still be edited via the raw command/args fields.
	const RUNNERS: { value: Runner; label: string; hint: string }[] = [
		{ value: 'npx', label: 'npx', hint: 'Node package (npm)' },
		{ value: 'uvx', label: 'uvx', hint: 'Python package (uv)' },
		{ value: 'command', label: 'command', hint: 'Any local binary' },
		{ value: 'remote', label: 'remote', hint: 'Remote HTTP/SSE URL' }
	];


	// ---- Form state -----------------------------------------------------------
	//
	// This is an *uncontrolled* form: `initial` seeds the fields once at mount and
	// the form owns its state thereafter (a later prop change won't clobber edits
	// in progress). We snapshot the seed in `untrack` so reading `initial` here
	// doesn't register a reactive dependency.

	function envToRows(env?: Record<string, string> | null): EnvRow[] {
		return Object.entries(env ?? {}).map(([key, value], i) => ({
			id: i,
			key,
			value
		}));
	}

	// Reverse-engineer the friendly `pkg` + extra-args fields from a stored raw
	// command/args (edit / import), so the form reads naturally. `-y` (npx) is
	// treated as boilerplate.
	const seed = untrack(() => {
		const init = initial ?? {};
		const runner0: Runner = init.runner ?? 'npx';
		const args0 = init.args ?? [];
		let pkg0 = '';
		let extra0 = '';
		if (runner0 === 'npx') {
			const rest = args0[0] === '-y' ? args0.slice(1) : args0.slice(0);
			pkg0 = rest[0] ?? '';
			extra0 = rest.slice(1).join('\n');
		} else if (runner0 === 'uvx') {
			pkg0 = args0[0] ?? '';
			extra0 = args0.slice(1).join('\n');
		}
		// remote stores [transport] in args (command holds the upstream URL).
		// Canonicalize through the shared map so an imported/aliased transport (e.g.
		// 'http') resolves to a value the <select> actually offers — never blank.
		const transport0 =
			runner0 === 'remote'
				? (canonicalRemoteTransport(args0[0]) ?? 'streamable-http')
				: 'streamable-http';
		return {
			name: init.name ?? '',
			slug: init.slug ?? '',
			runner: runner0,
			command: init.command ?? '',
			argsText: args0.join('\n'),
			pkg: pkg0,
			extraArgsText: extra0,
			transport: transport0,
			cwd: init.cwd ?? '',
			mcpHttp: init.mcp_http ?? true,
			restOpenapi: init.rest_openapi ?? false,
			startAfter: init.enabled ?? true,
			authProvider: init.auth_provider ?? 'inherit',
			envRows: envToRows(init.env)
		};
	});

	let name = $state(seed.name);
	// Slug is the public routing identity (/s/<slug>/). Editable only in edit mode;
	// `originalSlug` lets us warn (and skip the PATCH field) only on an actual rename.
	const originalSlug = seed.slug;
	let slug = $state(seed.slug);
	let runner = $state<Runner>(seed.runner);

	// Raw command + args are what the backend stores. In friendly mode they are
	// derived from `pkg` + `extraArgs`; the Advanced disclosure edits them
	// directly. `command` (for runner=command) is edited there too.
	let command = $state(seed.command);
	let argsText = $state(seed.argsText);

	// Friendly inputs (npx/uvx): the package/tool name, plus any extra args.
	let pkg = $state(seed.pkg);
	let extraArgsText = $state(seed.extraArgsText);

	// Remote runner: the upstream transport (command holds the URL; args = [transport]).
	let transport = $state(seed.transport);

	let cwd = $state(seed.cwd);
	let mcpHttp = $state(seed.mcpHttp);
	let restOpenapi = $state(seed.restOpenapi);
	let authProvider = $state<ServerAuthProvider>(seed.authProvider);
	let startAfter = $state(seed.startAfter);
	let advancedOpen = $state(false);

	let envRows = $state<EnvRow[]>(seed.envRows);
	let envSeq = seed.envRows.length;

	// ---- Sync helpers ---------------------------------------------------------

	function splitLines(text: string): string[] {
		return text
			.split('\n')
			.map((l) => l.trim())
			.filter((l) => l.length > 0);
	}

	// Build the raw command + args from the friendly fields. Called on every
	// friendly-field edit and on runner change so Advanced always reflects them.
	function syncFromFriendly() {
		const extra = splitLines(extraArgsText);
		if (runner === 'npx') {
			command = 'npx';
			argsText = ['-y', ...(pkg ? [pkg] : []), ...extra].join('\n');
		} else if (runner === 'uvx') {
			command = 'uvx';
			argsText = [...(pkg ? [pkg] : []), ...extra].join('\n');
		}
		// runner === 'command': command + argsText are edited directly.
	}

	function onRunnerChange() {
		// Re-derive raw config for the newly selected runner.
		if (runner === 'command') {
			// Leave whatever the user had; nothing to derive.
		} else {
			syncFromFriendly();
		}
	}

	function addEnvRow() {
		envRows = [...envRows, { id: envSeq++, key: '', value: '' }];
	}

	function removeEnvRow(id: number) {
		envRows = envRows.filter((r) => r.id !== id);
	}

	// ---- Derived preview + validity ------------------------------------------

	const resolvedArgs = $derived(splitLines(argsText));

	const previewCommand = $derived(
		[command, ...resolvedArgs]
			.filter((p) => p.length > 0)
			.map((p) => (/\s/.test(p) ? `"${p}"` : p))
			.join(' ')
	);

	const isRemote = $derived(runner === 'remote');
	const nameValid = $derived(name.trim().length > 0);
	const commandValid = $derived(command.trim().length > 0);
	// A remote server's "command" is an upstream URL. Parse it (rather than regex) so the
	// client-side rule matches the backend's normalize_remote: http(s) scheme + a real
	// hostname, no whitespace. This rejects hostless values like "https://:443/mcp" that
	// the regex let through and the backend would 400.
	function isValidRemoteUrl(value: string): boolean {
		const trimmed = value.trim();
		if (!trimmed || /\s/.test(trimmed)) return false;
		try {
			const url = new URL(trimmed);
			return (url.protocol === 'http:' || url.protocol === 'https:') && url.hostname.length > 0;
		} catch {
			return false;
		}
	}
	const remoteUrlValid = $derived(!isRemote || isValidRemoteUrl(command));

	// Mirror the backend's slugify so the operator sees the value that will actually
	// be stored (lowercased, non-alphanumerics collapsed to single dashes).
	function slugify(value: string): string {
		return (
			value
				.trim()
				.toLowerCase()
				.replace(/[^a-z0-9]+/g, '-')
				.replace(/^-+|-+$/g, '') || 'server'
		);
	}
	const normalizedSlug = $derived(slugify(slug));
	const slugChanged = $derived(mode === 'edit' && normalizedSlug !== originalSlug);

	const canSubmit = $derived(nameValid && commandValid && remoteUrlValid && !busy);

	function buildEnv(): Record<string, string> {
		const out: Record<string, string> = {};
		for (const row of envRows) {
			const k = row.key.trim();
			if (k) out[k] = row.value;
		}
		return out;
	}

	function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		if (!canSubmit) return;
		const payload: ServerCreate & { slug?: string } = {
			name: name.trim(),
			runner,
			command: command.trim(),
			// remote stores [transport] in args; there's no local process, so no cwd.
			args: isRemote ? [transport] : resolvedArgs,
			env: buildEnv(),
			cwd: isRemote ? null : cwd.trim() ? cwd.trim() : null,
			mcp_http: mcpHttp,
			rest_openapi: restOpenapi,
			auth_provider: authProvider
		};
		if (mode === 'create') payload.enabled = startAfter;
		// Only send a slug when it's actually a rename, so an unchanged edit doesn't
		// touch identity.
		if (slugChanged) payload.slug = normalizedSlug;
		onsubmit(payload);
	}

	const friendly = $derived(runner === 'npx' || runner === 'uvx');
	const pkgLabel = $derived(runner === 'uvx' ? 'Package / tool' : 'npm package');
	const pkgPlaceholder = $derived(
		runner === 'uvx'
			? 'mcp-server-time'
			: '@modelcontextprotocol/server-memory'
	);
</script>

<form class="flex flex-col gap-7" onsubmit={handleSubmit} novalidate>
	<!-- Name -->
	<div class="flex flex-col gap-2">
		<label for="srv-name" class="text-sm font-medium text-[var(--color-ink)]">
			Name
		</label>
		<input
			id="srv-name"
			type="text"
			bind:value={name}
			required
			autocomplete="off"
			spellcheck="false"
			placeholder="memory"
			class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
		/>
		<p class="text-xs text-[var(--color-ink-dim)]">
			A human label. The slug is derived from it automatically.
		</p>
	</div>

	<!-- Slug (edit only): the public routing identity -->
	{#if mode === 'edit'}
		<div class="flex flex-col gap-2">
			<label for="srv-slug" class="text-sm font-medium text-[var(--color-ink)]">
				Slug
			</label>
			<input
				id="srv-slug"
				type="text"
				bind:value={slug}
				autocomplete="off"
				spellcheck="false"
				placeholder="memory"
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
			<p class="text-xs text-[var(--color-ink-dim)]">
				Identifies the server in its URLs:
				<code class="font-mono text-[var(--color-ink-muted)]">/s/{normalizedSlug}/mcp</code>.
			</p>
			{#if slugChanged}
				<p
					class="flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs leading-relaxed"
					style="border-color: color-mix(in oklab, var(--color-state-failed) 30%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 8%, transparent); color: var(--color-ink-muted);"
				>
					<svg
						class="mt-0.5 size-4 shrink-0 text-[var(--color-state-failed)]"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
						<path d="M12 9v4M12 17h.01" />
					</svg>
					<span>
						Changing the slug from
						<code class="font-mono text-[var(--color-ink)]">{originalSlug}</code> to
						<code class="font-mono text-[var(--color-ink)]">{normalizedSlug}</code>
						changes this server's MCP URL. Any client already pointed at the
						old address (Claude Desktop, etc.) will need to be re-pointed.
					</span>
				</p>
			{/if}
		</div>
	{/if}

	<!-- Runner -->
	<fieldset class="flex flex-col gap-2 border-0 p-0">
		<legend class="mb-1 text-sm font-medium text-[var(--color-ink)]">Runner</legend>
		<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
			{#each RUNNERS as r (r.value)}
				<label
					class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition"
					style={runner === r.value
						? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
						: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
				>
					<span class="flex items-center gap-2">
						<input
							type="radio"
							name="runner"
							value={r.value}
							checked={runner === r.value}
							onchange={() => {
								runner = r.value;
								onRunnerChange();
							}}
							class="sr-only"
						/>
						<span
							class="font-mono text-sm font-semibold"
							style={runner === r.value
								? 'color: var(--color-accent);'
								: 'color: var(--color-ink);'}
						>
							{r.label}
						</span>
					</span>
					<span class="text-[11px] leading-tight text-[var(--color-ink-dim)]">
						{r.hint}
					</span>
				</label>
			{/each}
		</div>
	</fieldset>

	<!-- Friendly per-runner input -->
	{#if friendly}
		<div class="flex flex-col gap-2">
			<label
				for="srv-pkg"
				class="text-sm font-medium text-[var(--color-ink)]"
			>
				{pkgLabel}
			</label>
			<input
				id="srv-pkg"
				type="text"
				bind:value={pkg}
				oninput={syncFromFriendly}
				autocomplete="off"
				spellcheck="false"
				placeholder={pkgPlaceholder}
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
			<label
				for="srv-extra-args"
				class="mt-1 text-xs font-medium text-[var(--color-ink-muted)]"
			>
				Extra arguments <span class="text-[var(--color-ink-dim)]">(optional, one per line)</span>
			</label>
			<textarea
				id="srv-extra-args"
				bind:value={extraArgsText}
				oninput={syncFromFriendly}
				rows="2"
				spellcheck="false"
				placeholder="--port&#10;8000"
				class="resize-y rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			></textarea>
		</div>
	{:else if isRemote}
		<!-- runner === remote: an already-remote MCP URL we proxy. command = URL,
		     args = [transport], env = upstream headers. No local process. -->
		<div class="flex flex-col gap-2">
			<label for="srv-url" class="text-sm font-medium text-[var(--color-ink)]">
				Upstream URL
			</label>
			<input
				id="srv-url"
				type="url"
				inputmode="url"
				bind:value={command}
				autocomplete="off"
				spellcheck="false"
				placeholder="https://example.com/mcp"
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
			{#if command.trim() && !remoteUrlValid}
				<p class="text-xs text-[var(--color-state-failed)]">
					Enter an http(s):// URL to the remote MCP endpoint.
				</p>
			{/if}
			<label for="srv-transport" class="mt-1 text-xs font-medium text-[var(--color-ink-muted)]">
				Transport
			</label>
			<div class="relative inline-flex items-center">
				<select
					id="srv-transport"
					bind:value={transport}
					class="w-full cursor-pointer appearance-none rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 pr-9 text-sm text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
				>
					{#each REMOTE_TRANSPORTS as t (t.value)}
						<option value={t.value}>{t.label}</option>
					{/each}
				</select>
				<svg class="pointer-events-none absolute right-3 size-4 text-[var(--color-ink-dim)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="m6 9 6 6 6-6" />
				</svg>
			</div>
			<p class="text-xs text-[var(--color-ink-dim)]">
				mcpelevator proxies this endpoint and fronts it with its own auth. Add any
				upstream auth as <span class="font-medium">Headers</span> below.
			</p>
		</div>
	{:else}
		<!-- runner === command: command + args are the friendly fields -->
		<div class="flex flex-col gap-2">
			<label
				for="srv-command"
				class="text-sm font-medium text-[var(--color-ink)]"
			>
				Command
			</label>
			<input
				id="srv-command"
				type="text"
				bind:value={command}
				autocomplete="off"
				spellcheck="false"
				placeholder="/usr/local/bin/my-mcp-server"
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
			<label
				for="srv-args"
				class="mt-1 text-xs font-medium text-[var(--color-ink-muted)]"
			>
				Arguments <span class="text-[var(--color-ink-dim)]">(one per line)</span>
			</label>
			<textarea
				id="srv-args"
				bind:value={argsText}
				rows="3"
				spellcheck="false"
				placeholder="--config&#10;/etc/server.toml"
				class="resize-y rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			></textarea>
		</div>
	{/if}

	{#if isRemote}
		<!-- Remote endpoint preview (no local process to spawn) -->
		<div class="flex flex-col gap-1.5">
			<span class="text-xs font-medium text-[var(--color-ink-muted)]">Proxies</span>
			<div class="overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5">
				{#if command.trim()}
					<code class="font-mono text-xs whitespace-nowrap text-[var(--color-accent)]">
						{transport} → {command.trim()}
					</code>
				{:else}
					<code class="font-mono text-xs text-[var(--color-ink-dim)]">(URL not set)</code>
				{/if}
			</div>
		</div>
	{:else}
	<!-- Advanced disclosure: resolved raw command + args -->
	<div
		class="overflow-hidden rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)]"
	>
		<button
			type="button"
			onclick={() => (advancedOpen = !advancedOpen)}
			aria-expanded={advancedOpen}
			class="flex w-full items-center justify-between gap-3 px-3.5 py-3 text-left transition hover:bg-[var(--color-surface-2)]"
		>
			<span class="flex flex-col gap-0.5">
				<span class="text-sm font-medium text-[var(--color-ink)]">Advanced</span>
				<span class="text-xs text-[var(--color-ink-dim)]">
					Resolved <code class="font-mono">command</code> + <code class="font-mono">args</code> as stored
				</span>
			</span>
			<svg
				class="size-4 shrink-0 text-[var(--color-ink-muted)] transition-transform"
				style={advancedOpen ? 'transform: rotate(180deg);' : ''}
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>
		</button>

		{#if advancedOpen}
			<div
				class="flex flex-col gap-3 border-t border-[var(--color-line)] px-3.5 py-3.5"
			>
				<div class="flex flex-col gap-1.5">
					<label
						for="adv-command"
						class="text-xs font-medium text-[var(--color-ink-muted)]"
					>
						command
					</label>
					<input
						id="adv-command"
						type="text"
						bind:value={command}
						autocomplete="off"
						spellcheck="false"
						class="rounded-md border border-[var(--color-line)] bg-[var(--color-base)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
					/>
				</div>
				<div class="flex flex-col gap-1.5">
					<label
						for="adv-args"
						class="text-xs font-medium text-[var(--color-ink-muted)]"
					>
						args <span class="text-[var(--color-ink-dim)]">(one per line)</span>
					</label>
					<textarea
						id="adv-args"
						bind:value={argsText}
						rows="4"
						spellcheck="false"
						class="resize-y rounded-md border border-[var(--color-line)] bg-[var(--color-base)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
					></textarea>
				</div>
				{#if friendly}
					<p class="text-[11px] leading-relaxed text-[var(--color-ink-dim)]">
						Editing here overrides the friendly fields above. Changing the
						package field again will rebuild these.
					</p>
				{/if}
			</div>
		{/if}
	</div>

	<!-- Resolved command preview -->
	<div class="flex flex-col gap-1.5">
		<span class="text-xs font-medium text-[var(--color-ink-muted)]">Will run</span>
		<div
			class="overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5"
		>
			{#if previewCommand}
				<code class="font-mono text-xs whitespace-nowrap text-[var(--color-accent)]">
					{previewCommand}
				</code>
			{:else}
				<code class="font-mono text-xs text-[var(--color-ink-dim)]">
					(command not set)
				</code>
			{/if}
		</div>
	</div>
	{/if}

	<!-- Environment variables (upstream headers when remote) -->
	<fieldset class="flex flex-col gap-2 border-0 p-0">
		<div class="flex items-center justify-between">
			<legend class="text-sm font-medium text-[var(--color-ink)]">
				{isRemote ? 'Headers' : 'Environment'}
			</legend>
			<button
				type="button"
				onclick={addEnvRow}
				class="inline-flex items-center gap-1 rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2 py-1 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
			>
				<svg
					class="size-3.5"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2.5"
					stroke-linecap="round"
					aria-hidden="true"
				>
					<path d="M12 5v14M5 12h14" />
				</svg>
				Add
			</button>
		</div>
		{#if envRows.length === 0}
			<p
				class="rounded-lg border border-dashed border-[var(--color-line)] px-3 py-3 text-xs text-[var(--color-ink-dim)]"
			>
				{isRemote
					? 'No headers. Add upstream auth (e.g. Authorization) the remote endpoint needs.'
					: 'No environment variables. Add API keys or config the server needs.'}
			</p>
		{:else}
			<div class="flex flex-col gap-2">
				{#each envRows as row (row.id)}
					<div class="flex items-center gap-2">
						<input
							type="text"
							bind:value={row.key}
							autocomplete="off"
							spellcheck="false"
							placeholder="KEY"
							aria-label="Environment variable name"
							class="w-2/5 rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
						/>
						<input
							type="text"
							bind:value={row.value}
							autocomplete="off"
							spellcheck="false"
							placeholder="value"
							aria-label="Environment variable value"
							class="min-w-0 flex-1 rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
						/>
						<button
							type="button"
							onclick={() => removeEnvRow(row.id)}
							aria-label="Remove variable"
							class="shrink-0 rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] p-1.5 text-[var(--color-ink-dim)] transition hover:border-[var(--color-state-failed)] hover:text-[var(--color-state-failed)]"
						>
							<svg
								class="size-3.5"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								stroke-linecap="round"
								aria-hidden="true"
							>
								<path d="M18 6 6 18M6 6l12 12" />
							</svg>
						</button>
					</div>
				{/each}
			</div>
		{/if}
	</fieldset>

	<!-- Working directory (not applicable to a remote upstream) -->
	{#if !isRemote}
		<div class="flex flex-col gap-2">
			<label for="srv-cwd" class="text-sm font-medium text-[var(--color-ink)]">
				Working directory <span class="font-normal text-[var(--color-ink-dim)]">(optional)</span>
			</label>
			<input
				id="srv-cwd"
				type="text"
				bind:value={cwd}
				autocomplete="off"
				spellcheck="false"
				placeholder="/path/to/project"
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
		</div>
	{/if}

	<!-- Exposure toggles -->
	<fieldset class="flex flex-col gap-3 border-0 p-0">
		<legend class="mb-1 text-sm font-medium text-[var(--color-ink)]">Exposure</legend>
		<label
			class="flex cursor-pointer items-start justify-between gap-3 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3.5 py-3"
		>
			<span class="flex flex-col gap-0.5">
				<span class="text-sm font-medium text-[var(--color-ink)]">MCP over HTTP</span>
				<span class="text-xs text-[var(--color-ink-dim)]">
					Expose a streamable MCP endpoint.
				</span>
			</span>
			<input type="checkbox" bind:checked={mcpHttp} class="peer sr-only" />
			<span
				class="relative mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full transition peer-checked:bg-[var(--color-accent)] peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-[var(--color-accent)]"
				style="background-color: {mcpHttp ? '' : 'var(--color-line-strong)'};"
			>
				<span
					class="ml-0.5 inline-block size-4 rounded-full bg-white transition"
					style={mcpHttp ? 'transform: translateX(16px);' : ''}
				></span>
			</span>
		</label>
		<!-- A per-server REST/OpenAPI surface is planned (M6) but not served yet, so the
		     toggle is intentionally not offered. `rest_openapi` is preserved on existing
		     rows; the control returns when the backend ships. -->
	</fieldset>

	<!-- Auth -->
	<fieldset class="flex flex-col gap-2 border-0 p-0">
		<legend class="mb-1 text-sm font-medium text-[var(--color-ink)]">Auth</legend>
		<div
			class="grid grid-cols-3 gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-1"
		>
			{#each AUTH_OPTIONS as opt (opt.value)}
				<label
					class="flex cursor-pointer items-center justify-center rounded-md px-3 py-1.5 font-mono text-xs font-semibold transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
					style={authProvider === opt.value
						? 'background-color: color-mix(in oklab, var(--color-accent) 14%, transparent); color: var(--color-accent);'
						: 'color: var(--color-ink-muted);'}
				>
					<input
						type="radio"
						name="auth-provider"
						value={opt.value}
						checked={authProvider === opt.value}
						onchange={() => (authProvider = opt.value)}
						class="sr-only"
					/>
					{opt.label}
				</label>
			{/each}
		</div>
		<p class="text-xs text-[var(--color-ink-dim)]">
			<code class="font-mono">inherit</code> = use the global default. Set it on the
			<a
				href="/settings"
				class="text-[var(--color-ink-muted)] underline decoration-dotted underline-offset-2 transition hover:text-[var(--color-ink)]"
			>
				Settings
			</a> page.
		</p>
	</fieldset>

	<!-- Start after creating (create only) -->
	{#if mode === 'create'}
		<label
			class="flex cursor-pointer items-start justify-between gap-3 rounded-lg border px-3.5 py-3"
			style={startAfter
				? 'border-color: color-mix(in oklab, var(--color-accent) 40%, transparent); background-color: color-mix(in oklab, var(--color-accent) 7%, transparent);'
				: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
		>
			<span class="flex flex-col gap-0.5">
				<span class="text-sm font-medium text-[var(--color-ink)]">
					Start after creating
				</span>
				<span class="text-xs text-[var(--color-ink-dim)]">
					Boot the server immediately once it's added.
				</span>
			</span>
			<input type="checkbox" bind:checked={startAfter} class="peer sr-only" />
			<span
				class="relative mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full transition peer-checked:bg-[var(--color-accent)] peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-[var(--color-accent)]"
				style="background-color: {startAfter ? '' : 'var(--color-line-strong)'};"
			>
				<span
					class="ml-0.5 inline-block size-4 rounded-full bg-white transition"
					style={startAfter ? 'transform: translateX(16px);' : ''}
				></span>
			</span>
		</label>
	{:else}
		<p
			class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3.5 py-2.5 text-xs text-[var(--color-ink-muted)]"
		>
			Saving configuration changes restarts the server.
		</p>
	{/if}

	<!-- Error -->
	{#if error}
		<p
			role="alert"
			class="rounded-lg border px-3.5 py-3 font-mono text-xs leading-relaxed"
			style="border-color: color-mix(in oklab, var(--color-state-failed) 35%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 10%, transparent); color: var(--color-state-failed);"
		>
			{error}
		</p>
	{/if}

	<!-- Actions -->
	<div class="flex items-center justify-end gap-2 pt-1">
		{#if oncancel}
			<button
				type="button"
				onclick={oncancel}
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
			>
				Cancel
			</button>
		{/if}
		<button
			type="submit"
			disabled={!canSubmit}
			aria-busy={busy}
			class="inline-flex items-center gap-2 rounded-lg bg-[var(--color-accent)] px-5 py-2 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)] disabled:cursor-not-allowed disabled:opacity-50"
		>
			{#if busy}
				<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
					<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
					<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
				</svg>
			{:else}
				<RunnerBadge {runner} />
			{/if}
			{submitLabel ?? (mode === 'create' ? 'Create server' : 'Save changes')}
		</button>
	</div>
</form>
