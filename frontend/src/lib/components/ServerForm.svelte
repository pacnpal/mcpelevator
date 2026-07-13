<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import RunnerBadge from './RunnerBadge.svelte';
	import { getSettings } from '$lib/api';
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
		oauthHasSecret = false,
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
		/** Edit mode: whether a static OAuth client secret is already stored. The secret
		 * value is never returned, so the field starts blank and, if left blank, is omitted
		 * from the PATCH (kept) rather than cleared. */
		oauthHasSecret?: boolean;
		busy?: boolean;
		/** API error text to surface inline, if any. */
		error?: string | null;
		submitLabel?: string;
		/** Receives a fully-built ServerCreate-shaped payload. In edit mode it also
		 * carries `slug` when the operator renamed it. */
		onsubmit: (payload: ServerCreate & { slug?: string }) => void;
		oncancel?: () => void;
	} = $props();

	// The runners offered in the form. For `docker` the stored shape is the OCI image ref
	// (command) + the container's own args; the backend synthesizes the hardened
	// `docker run …`. The docker runner is opt-in + root-equivalent — gated below.
	const RUNNERS: { value: Runner; label: string; hint: string }[] = [
		{ value: 'npx', label: 'npx', hint: 'Node package (npm)' },
		{ value: 'uvx', label: 'uvx', hint: 'Python package (uv)' },
		{ value: 'command', label: 'command', hint: 'Any local binary' },
		{ value: 'docker', label: 'docker', hint: 'Docker / OCI image' },
		{ value: 'remote', label: 'remote', hint: 'Remote HTTP/SSE URL' }
	];

	// Whether the (root-equivalent, opt-in) docker runner is enabled. Fetched once; when
	// off we surface a banner and block submitting a docker server. `null` = not yet known.
	let dockerEnabled = $state<boolean | null>(null);
	onMount(() => {
		getSettings()
			.then((s) => (dockerEnabled = s.docker_runner))
			.catch(() => (dockerEnabled = null));
	});


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
			oauth: init.oauth ?? false,
			oauthScopes: init.oauth_scopes ?? '',
			oauthClientId: init.oauth_client_id ?? '',
			oauthClientSecret: init.oauth_client_secret ?? '',
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

	// Remote runner upstream auth: static Headers (the default) vs OAuth. When OAuth is
	// on, mcpelevator runs the provider sign-in and stores/refreshes tokens; scopes and
	// static client credentials are optional (empty client id = Dynamic Client Registration).
	let oauth = $state(seed.oauth);
	let oauthScopes = $state(seed.oauthScopes);
	let oauthClientId = $state(seed.oauthClientId);
	let oauthClientSecret = $state(seed.oauthClientSecret);
	// Open the client-credentials disclosure when there's already an id or a stored secret.
	// Initial capture only (uncontrolled form), so untrack the prop read.
	let oauthClientOpen = $state(untrack(() => !!seed.oauthClientId || oauthHasSecret));
	// Edit mode only: explicitly remove a stored (write-only) secret while keeping the client id
	// — e.g. switching a confidential client to a public one. Blank alone means "keep".
	let oauthRemoveSecret = $state(false);

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
		if (runner === 'command' || runner === 'docker') {
			// command/docker edit command + args directly; nothing to derive.
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

	// Quote a preview token only if it contains whitespace (shared by both previews below).
	function quoteIfNeeded(p: string): string {
		return /\s/.test(p) ? `"${p}"` : p;
	}

	const previewCommand = $derived(
		[command, ...resolvedArgs].filter((p) => p.length > 0).map(quoteIfNeeded).join(' ')
	);

	const isRemote = $derived(runner === 'remote');
	const isDocker = $derived(runner === 'docker');
	// A docker server is only *started* when created with "Start after creating" on; editing
	// or saving a disabled docker server is always allowed (the backend gates on enable, not
	// on storing a disabled row) so an imported config can be reviewed first.
	const dockerDisabled = $derived(isDocker && dockerEnabled === false);
	const dockerBlocked = $derived(dockerDisabled && mode === 'create' && startAfter);
	const nameValid = $derived(name.trim().length > 0);
	const commandValid = $derived(command.trim().length > 0);

	// The hardened `docker run …` the backend will synthesize from the image + args (an
	// honest preview; the exact hardening flags are elided as […]).
	const dockerPreview = $derived(
		[
			// static prefix (kept verbatim — it isn't user input), then the user-provided
			// image + args, each quoted only if it contains whitespace.
			'docker run -i --rm --init […]',
			...[command.trim() || '<image>', ...resolvedArgs].filter((p) => p.length > 0).map(quoteIfNeeded)
		].join(' ')
	);
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

	const canSubmit = $derived(
		nameValid && commandValid && remoteUrlValid && !dockerBlocked && !busy
	);

	function buildEnv(): Record<string, string> {
		const out: Record<string, string> = {};
		for (const row of envRows) {
			const k = row.key.trim();
			if (k) out[k] = row.value;
		}
		return out;
	}

	// The client secret is write-only: not returned by the API, so the field starts blank.
	// A typed value is sent; blank in edit mode with an existing secret is OMITTED (kept),
	// otherwise null (no secret).
	function oauthSecretPayload(): string | null | undefined {
		if (!(isRemote && oauth)) return null;
		// A secret is only meaningful with a client id. If the id is cleared (switching a
		// static client back to Dynamic Client Registration), clear the secret too —
		// otherwise the PATCH would omit it, the backend would keep the old secret, and it
		// would reject the update as a secret-without-id.
		if (!oauthClientId.trim()) return null;
		// "Remove" wins over any typed value FIRST: the field is disabled while the box is
		// ticked, but a secret typed before ticking it would otherwise linger in state and
		// get sent — resurrecting the very secret the operator asked to drop.
		if (mode === 'edit' && oauthHasSecret && oauthRemoveSecret) return null;
		// Send the secret VERBATIM — it's an opaque credential, so don't .trim() the value
		// (a provider secret may legitimately carry edge whitespace); only use a trimmed
		// check to tell "typed something" from "left blank".
		if (oauthClientSecret.trim()) return oauthClientSecret;
		// Blank: keep the stored secret in edit mode; otherwise no secret.
		if (mode === 'edit' && oauthHasSecret) return undefined;
		return null;
	}

	function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		if (!canSubmit) return;
		const payload: ServerCreate & { slug?: string } = {
			name: name.trim(),
			runner,
			command: command.trim(),
			// remote stores [transport] in args; there's no local process, so no cwd.
			// docker stores command=image + args=container args, and cwd is meaningless.
			args: isRemote ? [transport] : resolvedArgs,
			env: buildEnv(),
			cwd: isRemote || isDocker ? null : cwd.trim() ? cwd.trim() : null,
			mcp_http: mcpHttp,
			rest_openapi: restOpenapi,
			auth_provider: authProvider,
			// Upstream OAuth only applies to remote; the backend forces it off elsewhere,
			// but keep the payload honest so a runner switch clears it client-side too.
			oauth: isRemote && oauth,
			oauth_scopes: isRemote && oauth ? oauthScopes.trim() : '',
			oauth_client_id: isRemote && oauth ? oauthClientId.trim() || null : null,
			oauth_client_secret: oauthSecretPayload()
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
				mcpelevator proxies this endpoint and fronts it with its own auth. Choose how it
				authenticates <span class="font-medium">to the upstream</span> below.
			</p>

			<!-- Upstream authentication: static Headers vs OAuth sign-in. -->
			<span class="mt-1 text-xs font-medium text-[var(--color-ink-muted)]">
				Upstream authentication
			</span>
			<div class="grid grid-cols-2 gap-2">
				<label
					class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition"
					style={!oauth
						? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
						: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
				>
					<span class="flex items-center gap-2">
						<input type="radio" name="upstream-auth" checked={!oauth} onchange={() => (oauth = false)} />
						<span class="text-sm font-medium text-[var(--color-ink)]">Headers</span>
					</span>
					<span class="text-xs text-[var(--color-ink-dim)]">API key / static bearer token</span>
				</label>
				<label
					class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition"
					style={oauth
						? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
						: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
				>
					<span class="flex items-center gap-2">
						<input type="radio" name="upstream-auth" checked={oauth} onchange={() => (oauth = true)} />
						<span class="text-sm font-medium text-[var(--color-ink)]">OAuth</span>
					</span>
					<span class="text-xs text-[var(--color-ink-dim)]">Sign in with the provider</span>
				</label>
			</div>

			<!-- Which to pick: most servers support both, but not all. -->
			<p class="text-xs leading-relaxed text-[var(--color-ink-dim)]">
				Most remote MCP servers accept <span class="font-medium">both</span> a static token
				header and OAuth, but some support only one — check the server's docs to be sure. A
				long-lived <span class="font-medium">token/API key</span> (under Headers) is the more
				permanent option; <span class="font-medium">OAuth</span> is best when the provider
				requires it or doesn't issue static tokens.
			</p>

			{#if oauth}
				<div class="mt-1 flex flex-col gap-3 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-3">
					<p class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
						{#if mode === 'create'}
							After you create this server, open it and click
							<span class="font-medium text-[var(--color-ink)]">Authenticate with provider</span>
							to sign in — mcpelevator stores the tokens and refreshes them automatically.
						{:else}
							Use <span class="font-medium text-[var(--color-ink)]">Authenticate with provider</span>
							on the server page to sign in — mcpelevator stores the tokens and refreshes them
							automatically.
						{/if}
					</p>
					<p
						class="flex items-start gap-2 text-xs leading-relaxed text-[var(--color-ink-muted)]"
					>
						<svg class="mt-0.5 size-4 shrink-0 text-[var(--color-ink-dim)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
							<circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
						</svg>
						<span>
							OAuth sessions can expire. Refresh is automatic, but if the provider's refresh
							window lapses you'll need to re-authenticate here to keep the server working —
							check on it periodically.
						</span>
					</p>

					<div class="flex flex-col gap-2">
						<label for="srv-oauth-scopes" class="text-xs font-medium text-[var(--color-ink-muted)]">
							Scopes <span class="text-[var(--color-ink-dim)]">(optional, space-separated)</span>
						</label>
						<input
							id="srv-oauth-scopes"
							type="text"
							bind:value={oauthScopes}
							autocomplete="off"
							spellcheck="false"
							placeholder="read write"
							class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
						/>
						<p class="text-xs text-[var(--color-ink-dim)]">
							Leave blank to request whatever scopes the provider advertises.
						</p>
					</div>

					<!-- Static client credentials: optional. Blank = Dynamic Client Registration. -->
					<div class="flex flex-col gap-2">
						<button
							type="button"
							onclick={() => (oauthClientOpen = !oauthClientOpen)}
							class="flex w-fit items-center gap-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
						>
							<svg class="size-3.5 transition-transform" style={oauthClientOpen ? 'transform: rotate(90deg);' : ''} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								<path d="m9 18 6-6-6-6" />
							</svg>
							Client credentials (advanced)
						</button>
						{#if oauthClientOpen}
							<div class="flex flex-col gap-2">
								<input
									type="text"
									aria-label="OAuth client ID"
									bind:value={oauthClientId}
									autocomplete="off"
									spellcheck="false"
									placeholder="Client ID — optional, blank auto-registers"
									class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
								/>
								<input
									type="password"
									aria-label="OAuth client secret"
									bind:value={oauthClientSecret}
									disabled={mode === 'edit' && oauthHasSecret && oauthRemoveSecret}
									autocomplete="off"
									spellcheck="false"
									placeholder={oauthHasSecret ? 'Set — leave blank to keep current' : 'Client secret — optional'}
									class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
								/>
								<p class="text-xs text-[var(--color-ink-dim)]">
									Leave blank to register automatically (Dynamic Client Registration). Fill these
									in only if the provider issued you a pre-registered client.
								</p>
								{#if mode === 'edit' && oauthHasSecret}
									<label class="flex items-center gap-2 text-xs text-[var(--color-ink-muted)]">
										<input type="checkbox" bind:checked={oauthRemoveSecret} />
										Remove the stored client secret (switch to a public client)
									</label>
								{/if}
							</div>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{:else if isDocker}
		<!-- runner === docker: command = OCI image ref, args = the container's own args.
		     The backend synthesizes the hardened `docker run …` (see dockerPreview). -->
		<div class="flex flex-col gap-2">
			{#if dockerDisabled}
				<p
					class="flex items-start gap-2 rounded-lg border px-3 py-2.5 text-xs leading-relaxed"
					style="border-color: color-mix(in oklab, var(--color-state-failed) 30%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 8%, transparent); color: var(--color-ink-muted);"
				>
					<svg class="mt-0.5 size-4 shrink-0 text-[var(--color-state-failed)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
						<path d="M12 9v4M12 17h.01" />
					</svg>
					<span>
						The Docker runner is disabled (it's root-equivalent). You can still
						{mode === 'create' ? 'save this server disabled' : 'edit and save it'} for review — but
						enable the runner on the
						<a href="/settings" class="font-medium text-[var(--color-ink)] underline decoration-dotted underline-offset-2">Settings</a>
						page before starting it.
						{#if dockerBlocked}
							<span class="font-medium text-[var(--color-ink)]">Turn off “Start after creating” below to save it now.</span>
						{/if}
					</span>
				</p>
			{/if}
			<label for="srv-image" class="text-sm font-medium text-[var(--color-ink)]">
				Image
			</label>
			<input
				id="srv-image"
				type="text"
				bind:value={command}
				autocomplete="off"
				spellcheck="false"
				placeholder="ghcr.io/github/github-mcp-server"
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
			<label for="srv-docker-args" class="mt-1 text-xs font-medium text-[var(--color-ink-muted)]">
				Container arguments <span class="text-[var(--color-ink-dim)]">(optional, one per line)</span>
			</label>
			<textarea
				id="srv-docker-args"
				bind:value={argsText}
				rows="2"
				spellcheck="false"
				placeholder="--toolsets&#10;repos"
				class="resize-y rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-xs text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			></textarea>
			<p class="text-xs text-[var(--color-ink-dim)]">
				mcpelevator adds the hardening, cleanup and secret-passing flags. Put env/API keys
				under <span class="font-medium">Environment</span> below — they're passed to the
				container by name (never embedded in the command).
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
	{:else if isDocker}
		<!-- Docker: show the hardened `docker run …` the backend synthesizes. -->
		<div class="flex flex-col gap-1.5">
			<span class="text-xs font-medium text-[var(--color-ink-muted)]">Will run</span>
			<div class="overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5">
				<code class="font-mono text-xs whitespace-nowrap text-[var(--color-accent)]">
					{dockerPreview}
				</code>
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
				{isRemote ? (oauth ? 'Extra headers' : 'Headers') : 'Environment'}
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
					? oauth
						? 'No extra headers. OAuth supplies the Authorization header; add others only if the upstream needs them.'
						: 'No headers. Add upstream auth (e.g. Authorization) the remote endpoint needs.'
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

	<!-- Working directory (not applicable to a remote upstream or a docker container) -->
	{#if !isRemote && !isDocker}
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
