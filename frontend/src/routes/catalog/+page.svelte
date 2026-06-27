<script lang="ts">
	import { goto } from '$app/navigation';
	import {
		errorMessage,
		getCatalogServer,
		listCatalog,
		listCatalogSources
	} from '$lib/api';
	import { setPendingInstall } from '$lib/catalogInstall';
	import RunnerBadge from '$lib/components/RunnerBadge.svelte';
	import Toast from '$lib/components/Toast.svelte';
	import type { CatalogServer, CatalogSource, Runner } from '$lib/types';

	// ---- Toast ----------------------------------------------------------------
	let toast = $state<{ message: string; tone: 'error' | 'info' } | null>(null);
	let toastTimer: ReturnType<typeof setTimeout> | undefined;
	function flashToast(message: string, tone: 'error' | 'info' = 'error') {
		toast = { message, tone };
		clearTimeout(toastTimer);
		toastTimer = setTimeout(() => (toast = null), 6000);
	}

	// ---- Sources --------------------------------------------------------------
	let sources = $state<CatalogSource[]>([]);
	let source = $state('official');
	const activeSource = $derived(sources.find((s) => s.id === source));

	// ---- Browse state ---------------------------------------------------------
	let search = $state('');
	let servers = $state<CatalogServer[]>([]);
	let nextCursor = $state<string | null>(null);
	let loading = $state(true);
	let loadingMore = $state(false);
	let installing = $state<string | null>(null); // id currently being resolved
	let error = $state<string | null>(null);

	// A monotonically increasing token guards against out-of-order responses: a
	// stale in-flight query (slow network) can't clobber a newer one's results.
	let queryToken = 0;

	let searchTimer: ReturnType<typeof setTimeout> | undefined;

	$effect(() => {
		void load();
		return () => {
			clearTimeout(searchTimer);
			clearTimeout(toastTimer);
		};
	});

	async function load() {
		try {
			sources = await listCatalogSources();
			if (!sources.some((s) => s.id === source) && sources.length) {
				source = sources[0].id;
			}
		} catch {
			// Non-fatal: fall back to the default source if the descriptor call fails.
		}
		await runSearch();
	}

	async function runSearch() {
		const token = ++queryToken;
		loading = true;
		error = null;
		try {
			const res = await listCatalog({ source, search: search.trim() || undefined });
			if (token !== queryToken) return; // a newer query superseded this one
			servers = res.servers;
			nextCursor = res.next_cursor;
		} catch (err) {
			if (token !== queryToken) return;
			error = errorMessage(err);
			servers = [];
			nextCursor = null;
		} finally {
			if (token === queryToken) loading = false;
		}
	}

	function onSearchInput() {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(runSearch, 300);
	}

	function selectSource(id: string) {
		if (id === source) return;
		source = id;
		nextCursor = null;
		runSearch();
	}

	async function loadMore() {
		if (!nextCursor || loadingMore) return;
		const token = queryToken; // don't append if the query changed mid-flight
		loadingMore = true;
		try {
			const res = await listCatalog({
				source,
				search: search.trim() || undefined,
				cursor: nextCursor
			});
			if (token !== queryToken) return;
			servers = [...servers, ...res.servers];
			nextCursor = res.next_cursor;
		} catch (err) {
			flashToast(errorMessage(err), 'error');
		} finally {
			loadingMore = false;
		}
	}

	// Resolve the chosen server's drafts, stash a PendingInstall, and route to the
	// review form. Picks the first installable package; if none, hands off the first
	// (manual) draft so discovery-only sources still scaffold the form.
	async function install(server: CatalogServer) {
		if (installing) return;
		installing = server.id;
		try {
			const detail = await getCatalogServer(server.id, server.source);
			const drafts = detail.drafts;
			const draft = drafts.find((d) => d.installable) ?? drafts[0] ?? null;
			const supportMeta = sources.find((s) => s.id === server.source);
			const versionTag = detail.server.version ? `@${detail.server.version}` : '';

			setPendingInstall({
				initial: {
					name: detail.server.title || detail.server.name,
					runner: (draft?.runner ?? 'npx') as Runner,
					command: draft?.command ?? '',
					args: draft?.args ?? [],
					env: draft?.env ?? {}
				},
				source: `catalog:${detail.server.name}${versionTag}`,
				sourceLabel: supportMeta?.label ?? server.source,
				installSupport: detail.manual_install ? 'manual' : 'auto',
				warnings: draft?.warnings ?? [],
				notes: detail.notes,
				repositoryUrl: detail.server.repository_url,
				webUrl: detail.server.web_url
			});
			await goto('/catalog/install');
		} catch (err) {
			flashToast(errorMessage(err), 'error');
			installing = null;
		}
	}

	function statusTone(status: string): string {
		if (status === 'deprecated' || status === 'deleted') return 'var(--color-state-failed)';
		return 'var(--color-ink-dim)';
	}
</script>

<section class="flex w-full flex-col gap-7">
	<!-- Back -->
	<a
		href="/"
		class="inline-flex items-center gap-1.5 self-start text-sm text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
	>
		<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
			<path d="M19 12H5M12 19l-7-7 7-7" />
		</svg>
		Back to servers
	</a>

	<!-- Heading -->
	<div>
		<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">Browse the registry</h1>
		<p class="mt-1 text-sm text-[var(--color-ink-muted)]">
			Search public MCP directories and install a server with one review.
		</p>
	</div>

	<!-- Source tabs -->
	{#if sources.length > 1}
		<div
			class="inline-flex w-full gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-1 sm:w-auto sm:self-start"
			role="tablist"
			aria-label="Catalog source"
		>
			{#each sources as s (s.id)}
				<button
					type="button"
					role="tab"
					aria-selected={source === s.id}
					onclick={() => selectSource(s.id)}
					class="flex-1 rounded-md px-4 py-1.5 text-sm font-medium transition sm:flex-none"
					style={source === s.id
						? 'background-color: var(--color-elevated); color: var(--color-ink);'
						: 'color: var(--color-ink-muted);'}
				>
					{s.label}
				</button>
			{/each}
		</div>
	{/if}

	<!-- Search -->
	<div class="flex flex-col gap-2">
		<div class="relative">
			<svg class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[var(--color-ink-dim)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<circle cx="11" cy="11" r="8" />
				<path d="m21 21-4.3-4.3" />
			</svg>
			<input
				type="search"
				bind:value={search}
				oninput={onSearchInput}
				placeholder="Search MCP servers…"
				aria-label="Search the catalog"
				class="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] py-2.5 pl-9 pr-3 text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			/>
		</div>
		{#if activeSource?.install_support === 'manual'}
			<p class="text-xs text-[var(--color-ink-dim)]">
				{activeSource.label} is a discovery directory — it has no launch command, so installs open the form pre-filled for you to complete manually.
			</p>
		{/if}
	</div>

	<!-- Results -->
	{#if loading}
		<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each Array(6) as _, i (i)}
				<div class="h-36 animate-pulse rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)]"></div>
			{/each}
		</div>
	{:else if error}
		<div class="flex flex-col items-start gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-6">
			<p class="text-sm text-[var(--color-state-failed)]">{error}</p>
			<button type="button" onclick={runSearch} class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)]">
				Retry
			</button>
		</div>
	{:else if servers.length === 0}
		<div class="rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-10 text-center">
			<p class="text-sm text-[var(--color-ink-muted)]">No servers match your search.</p>
		</div>
	{:else}
		<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each servers as server (server.source + ':' + server.id)}
				<div class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-4">
					<div class="flex items-start justify-between gap-2">
						<div class="min-w-0">
							<h3 class="truncate text-sm font-semibold text-[var(--color-ink)]" title={server.name}>
								{server.title || server.name}
							</h3>
							<p class="truncate font-mono text-[11px] text-[var(--color-ink-dim)]" title={server.name}>
								{server.name}
							</p>
						</div>
						<div class="flex shrink-0 items-center gap-1">
							{#each server.registry_types as rt (rt)}
								{#if rt === 'npm'}
									<RunnerBadge runner="npx" />
								{:else if rt === 'pypi'}
									<RunnerBadge runner="uvx" />
								{/if}
							{/each}
						</div>
					</div>

					<p class="line-clamp-3 flex-1 text-xs leading-relaxed text-[var(--color-ink-muted)]">
						{server.description}
					</p>

					<div class="flex items-center justify-between gap-2">
						<div class="flex items-center gap-2 text-[11px]">
							{#if server.version}
								<span class="font-mono text-[var(--color-ink-dim)]">v{server.version}</span>
							{/if}
							{#if server.status && server.status !== 'active'}
								<span style="color: {statusTone(server.status)}">{server.status}</span>
							{/if}
						</div>
						<button
							type="button"
							onclick={() => install(server)}
							disabled={installing === server.id}
							class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition active:translate-y-px disabled:opacity-60"
							style="background-color: var(--color-accent); color: var(--color-accent-ink);"
						>
							{#if installing === server.id}
								<svg class="size-3.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8V0C5.4 0 0 5.4 0 12h4z" />
								</svg>
								Resolving…
							{:else if server.installable}
								Install
							{:else}
								Set up
							{/if}
						</button>
					</div>
				</div>
			{/each}
		</div>

		{#if nextCursor}
			<button
				type="button"
				onclick={loadMore}
				disabled={loadingMore}
				class="mx-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-5 py-2 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)] disabled:opacity-60"
			>
				{loadingMore ? 'Loading…' : 'Load more'}
			</button>
		{/if}
	{/if}
</section>

<!-- Toast -->
{#if toast}
	<div
		class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:justify-end sm:px-6"
	>
		<div class="w-full max-w-sm">
			<Toast message={toast.message} tone={toast.tone} onclose={() => (toast = null)} />
		</div>
	</div>
{/if}
