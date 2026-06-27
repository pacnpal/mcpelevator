<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import {
		cloneServer,
		deleteServer,
		disableServer,
		enableServer,
		errorMessage,
		getServer,
		getSettings
	} from '$lib/api';
	import type { AuthProvider, ServerDetail } from '$lib/types';
	import CopyButton from '$lib/components/CopyButton.svelte';
	import LogViewer from '$lib/components/LogViewer.svelte';
	import RunnerBadge from '$lib/components/RunnerBadge.svelte';
	import StatePill from '$lib/components/StatePill.svelte';
	import Toast from '$lib/components/Toast.svelte';

	type LoadState = 'loading' | 'ready' | 'error';

	const id = $derived(page.params.id ?? '');

	let server = $state<ServerDetail | null>(null);
	let loadState = $state<LoadState>('loading');
	let loadError = $state<string | null>(null);

	// Global default auth, used to resolve a server set to `inherit` so the
	// endpoint hint reflects the *effective* auth. Best-effort; ignore failures.
	let defaultAuth = $state<AuthProvider | null>(null);

	let busy = $state(false); // enable/disable in flight
	let deleting = $state(false);
	let confirmDelete = $state(false);
	let cloning = $state(false);

	let toast = $state<string | null>(null);
	let toastTimer: ReturnType<typeof setTimeout> | undefined;
	function flashToast(message: string) {
		toast = message;
		clearTimeout(toastTimer);
		toastTimer = setTimeout(() => (toast = null), 6000);
	}

	async function load(silent = false) {
		// Capture the id this request is for. A clone navigates /server/[id] ->
		// /server/[id] (same route, reused component), so an in-flight request from
		// the source page (initial load or silent poll) can resolve *after* the
		// copy's load — drop it instead of clobbering `server` with the source.
		const requestedId = id;
		if (!silent) loadState = 'loading';
		try {
			const result = await getServer(requestedId);
			if (requestedId !== id) return; // route changed mid-flight; stale response
			server = result;
			loadState = 'ready';
			loadError = null;
		} catch (err) {
			if (requestedId !== id) return;
			if (!silent) {
				loadState = 'error';
				loadError = errorMessage(err);
			}
		}
	}

	const wantsRun = $derived(server?.enabled ?? false);

	async function toggle() {
		if (!server || busy) return;
		busy = true;
		try {
			server = wantsRun
				? { ...server, ...(await disableServer(server.id)) }
				: { ...server, ...(await enableServer(server.id)) };
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			busy = false;
		}
	}

	async function doClone() {
		if (!server || cloning) return;
		cloning = true;
		try {
			const copy = await cloneServer(server.id);
			// Land on the copy so the operator can review/edit, then enable it.
			await goto(`/server/${copy.id}`);
			// This is a same-route navigation (/server/[id] -> /server/[id]), so the
			// component instance is reused — clear the in-flight flag or the copy's
			// page would show the Clone button stuck disabled.
			cloning = false;
		} catch (err) {
			flashToast(errorMessage(err));
			cloning = false;
		}
	}

	async function doDelete() {
		if (!server || deleting) return;
		deleting = true;
		try {
			await deleteServer(server.id);
			await goto('/');
		} catch (err) {
			flashToast(errorMessage(err));
			deleting = false;
			confirmDelete = false;
		}
	}

	// Initial load + lightweight polling so live state (running/starting) stays
	// fresh while the page is open. Polls silently to avoid flicker.
	$effect(() => {
		// Re-run when the route id changes.
		void id;
		load();
		// Resolve the global default once so `inherit` servers show their
		// effective auth. Best-effort — endpoint hint just hides on failure.
		getSettings()
			.then((s) => (defaultAuth = s.default_auth_provider))
			.catch(() => {});
		const poll = setInterval(() => {
			if (loadState === 'ready' && !busy && !deleting) load(true);
		}, 4000);
		return () => {
			clearInterval(poll);
			clearTimeout(toastTimer);
		};
	});

	const envEntries = $derived(Object.entries(server?.env ?? {}));

	// Effective auth for the endpoint hint: `inherit` resolves to the global
	// default. `null` while the default is still unknown for an inherit server.
	const effectiveBearer = $derived(
		server?.auth_provider === 'bearer' ||
			(server?.auth_provider === 'inherit' && defaultAuth === 'bearer')
	);

	// Render the stored command + args as a single shell-ish line, quoting any
	// token that contains whitespace so the spacing reads correctly.
	const commandLine = $derived(
		[server?.command ?? '', ...(server?.args ?? [])]
			.filter((p) => p.length > 0)
			.map((p) => (/\s/.test(p) ? `"${p}"` : p))
			.join(' ')
	);
</script>

<section class="mx-auto flex w-full max-w-3xl flex-col gap-6">
	<!-- Back -->
	<a
		href="/"
		class="inline-flex items-center gap-1.5 self-start text-sm text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
	>
		<svg
			class="size-4"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M19 12H5M12 19l-7-7 7-7" />
		</svg>
		Back to servers
	</a>

	{#if loadState === 'loading'}
		<div
			class="flex items-center justify-center gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] px-6 py-20 text-sm text-[var(--color-ink-muted)]"
		>
			<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
				<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
				<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
			</svg>
			Loading server…
		</div>
	{:else if loadState === 'error'}
		<div
			class="flex flex-col items-center gap-4 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] bg-[var(--color-surface)] px-6 py-16 text-center"
		>
			<p class="text-base font-semibold text-[var(--color-ink)]">
				Couldn't load this server
			</p>
			<p class="max-w-sm font-mono text-xs text-[var(--color-state-failed)]">
				{loadError}
			</p>
			<button
				type="button"
				onclick={() => load()}
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)]"
			>
				Retry
			</button>
		</div>
	{:else if server}
		<!-- Header -->
		<div class="flex flex-wrap items-start justify-between gap-4">
			<div class="min-w-0">
				<div class="flex items-center gap-3">
					<h1
						class="truncate text-2xl font-semibold tracking-tight text-[var(--color-ink)]"
					>
						{server.name}
					</h1>
					<StatePill state={server.state} />
				</div>
				<p class="mt-1 truncate font-mono text-sm text-[var(--color-ink-dim)]">
					{server.slug}
				</p>
			</div>

			<div class="flex items-center gap-2">
				<button
					type="button"
					onclick={toggle}
					disabled={busy}
					aria-busy={busy}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-semibold transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
					style={wantsRun
						? 'color: var(--color-ink-muted); border: 1px solid var(--color-line);'
						: 'color: var(--color-accent-ink); background-color: var(--color-accent);'}
				>
					{#if busy}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						{wantsRun ? 'Stopping' : 'Starting'}
					{:else if wantsRun}
						<svg class="size-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<rect x="7" y="7" width="10" height="10" rx="1.5" />
						</svg>
						Stop
					{:else}
						<svg class="size-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<path d="M8 5v14l11-7z" />
						</svg>
						Start
					{/if}
				</button>

				<button
					type="button"
					onclick={doClone}
					disabled={cloning}
					aria-busy={cloning}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3.5 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70"
				>
					{#if cloning}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						Cloning
					{:else}
						<svg
							class="size-4"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<rect x="9" y="9" width="11" height="11" rx="2" />
							<path d="M5 15V5a2 2 0 0 1 2-2h10" />
						</svg>
						Clone
					{/if}
				</button>

				<a
					href={`/server/${server.id}/edit`}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3.5 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
				>
					<svg
						class="size-4"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M12 20h9" />
						<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
					</svg>
					Edit
				</a>
			</div>
		</div>

		<!-- last_error -->
		{#if server.last_error}
			<p
				class="rounded-lg border px-3.5 py-3 font-mono text-xs leading-relaxed"
				style="border-color: color-mix(in oklab, var(--color-state-failed) 30%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 10%, transparent); color: var(--color-state-failed);"
			>
				{server.last_error}
			</p>
		{/if}

		<!-- Endpoints -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<h2 class="text-sm font-semibold text-[var(--color-ink)]">Endpoints</h2>
			<div class="flex flex-col gap-2.5">
				<div class="flex items-center justify-between gap-3">
					<div class="min-w-0 flex-1">
						<p class="text-xs font-medium text-[var(--color-ink-muted)]">MCP</p>
						<p class="truncate font-mono text-xs text-[var(--color-ink)]">
							{server.urls.mcp ?? '— not exposed —'}
						</p>
					</div>
					<CopyButton value={server.urls.mcp} label="Copy" />
				</div>
				{#if server.transports.rest_openapi || server.urls.rest}
					<div class="flex items-center justify-between gap-3 border-t border-[var(--color-line)] pt-2.5">
						<div class="min-w-0 flex-1">
							<p class="text-xs font-medium text-[var(--color-ink-muted)]">REST</p>
							<p class="truncate font-mono text-xs text-[var(--color-ink)]">
								{server.urls.rest ?? '— not exposed —'}
							</p>
						</div>
					<CopyButton value={server.urls.rest} label="Copy" />
					</div>
				{/if}
			</div>
			{#if effectiveBearer}
				<p
					class="flex items-center gap-1.5 border-t border-[var(--color-line)] pt-2.5 text-xs text-[var(--color-ink-dim)]"
				>
					<svg
						class="size-3.5 shrink-0 text-[var(--color-accent)]"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<rect x="5" y="11" width="14" height="10" rx="2" />
						<path d="M8 11V7a4 4 0 0 1 8 0v4" />
					</svg>
					<span>
						Requests need <code class="font-mono text-[var(--color-ink-muted)]">Authorization: Bearer &lt;token&gt;</code>.
						Manage tokens in
						<a
							href="/settings"
							class="text-[var(--color-ink-muted)] underline decoration-dotted underline-offset-2 transition hover:text-[var(--color-ink)]"
						>
							Settings
						</a>.
					</span>
				</p>
			{/if}
		</div>

		<!-- Configuration -->
		<div
			class="flex flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<h2 class="text-sm font-semibold text-[var(--color-ink)]">Configuration</h2>

			<div class="flex flex-wrap items-center gap-2">
				<RunnerBadge runner={server.runner} />
				{#if typeof server.pid === 'number'}
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">pid {server.pid}</span>
				{/if}
				{#if typeof server.port === 'number'}
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">port {server.port}</span>
				{/if}
			</div>

			<div class="flex flex-col gap-1.5">
				<span class="text-xs font-medium text-[var(--color-ink-muted)]">Command</span>
				<div
					class="overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5"
				>
					<code class="font-mono text-xs whitespace-pre text-[var(--color-ink)]">{commandLine}</code>
				</div>
			</div>

			{#if server.cwd}
				<div class="flex flex-col gap-1.5">
					<span class="text-xs font-medium text-[var(--color-ink-muted)]">Working directory</span>
					<code class="font-mono text-xs text-[var(--color-ink)]">{server.cwd}</code>
				</div>
			{/if}

			<div class="flex flex-col gap-1.5">
				<span class="text-xs font-medium text-[var(--color-ink-muted)]">Environment</span>
				{#if envEntries.length === 0}
					<p class="text-xs text-[var(--color-ink-dim)]">No environment variables.</p>
				{:else}
					<dl class="flex flex-col gap-1">
						{#each envEntries as [k, v] (k)}
							<div class="flex gap-2 font-mono text-xs">
								<dt class="shrink-0 text-[var(--color-accent)]">{k}</dt>
								<dd class="truncate text-[var(--color-ink-muted)]">{v}</dd>
							</div>
						{/each}
					</dl>
				{/if}
			</div>

			<div class="flex flex-wrap gap-x-6 gap-y-1.5 border-t border-[var(--color-line)] pt-3 text-xs">
				<span class="text-[var(--color-ink-dim)]">
					Source <span class="font-mono text-[var(--color-ink-muted)]">{server.source}</span>
				</span>
				<span class="text-[var(--color-ink-dim)]">
					Auth <span class="font-mono text-[var(--color-ink-muted)]">{server.auth_provider}</span>
				</span>
			</div>
		</div>

		<!-- Tools -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<div class="flex items-center justify-between">
				<h2 class="text-sm font-semibold text-[var(--color-ink)]">Tools</h2>
				<span class="font-mono text-xs text-[var(--color-ink-dim)]">{server.tools_count}</span>
			</div>
			{#if server.tools.length === 0}
				<p class="text-xs text-[var(--color-ink-dim)]">
					{server.state === 'running'
						? 'No tools discovered.'
						: 'Tools are discovered once the server is running.'}
				</p>
			{:else}
				<ul class="flex flex-col divide-y divide-[var(--color-line)]">
					{#each server.tools as tool (tool.name)}
						<li class="flex flex-col gap-0.5 py-2 first:pt-0 last:pb-0">
							<span class="font-mono text-xs font-medium text-[var(--color-ink)]">
								{tool.name}
							</span>
							{#if tool.description}
								<span class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
									{tool.description}
								</span>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</div>

		<!-- Logs -->
		<LogViewer serverId={server.id} serverState={server.state} />

		<!-- Danger zone -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border px-5 py-4"
			style="border-color: color-mix(in oklab, var(--color-state-failed) 25%, var(--color-line));"
		>
			{#if !confirmDelete}
				<div class="flex flex-wrap items-center justify-between gap-3">
					<div>
						<p class="text-sm font-medium text-[var(--color-ink)]">Delete server</p>
						<p class="text-xs text-[var(--color-ink-dim)]">
							Stops the server and removes it permanently.
						</p>
					</div>
					<button
						type="button"
						onclick={() => (confirmDelete = true)}
						class="shrink-0 rounded-lg border px-3.5 py-2 text-sm font-medium transition active:translate-y-px"
						style="border-color: color-mix(in oklab, var(--color-state-failed) 40%, transparent); color: var(--color-state-failed);"
					>
						Delete
					</button>
				</div>
			{:else}
				<div class="flex flex-col gap-3">
					<p class="text-sm text-[var(--color-ink)]">
						Delete <span class="font-semibold">{server.name}</span>? This stops and
						removes it.
					</p>
					<div class="flex items-center gap-2">
						<button
							type="button"
							onclick={doDelete}
							disabled={deleting}
							aria-busy={deleting}
							class="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
							style="background-color: var(--color-state-failed);"
						>
							{#if deleting}
								<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
									<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
									<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
								</svg>
								Deleting
							{:else}
								Yes, delete
							{/if}
						</button>
						<button
							type="button"
							onclick={() => (confirmDelete = false)}
							disabled={deleting}
							class="rounded-lg border border-[var(--color-line)] px-4 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
						>
							Cancel
						</button>
					</div>
				</div>
			{/if}
		</div>
	{/if}
</section>

<!-- Toast -->
{#if toast}
	<div
		class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:justify-end sm:px-6"
	>
		<div class="w-full max-w-sm">
			<Toast message={toast} onclose={() => (toast = null)} />
		</div>
	</div>
{/if}
