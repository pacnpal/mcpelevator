<script lang="ts">
	import { goto } from '$app/navigation';
	import {
		errorMessage,
		getCatalogServer,
		getCatalogVersions,
		listCatalog,
		listCatalogSources
	} from '$lib/api';
	import { setPendingInstall } from '$lib/catalogInstall';
	import { canonicalRemoteTransport } from '$lib/remote';
	import RunnerBadge from '$lib/components/RunnerBadge.svelte';
	import { flashToast } from '$lib/toast.svelte';
	import type { CatalogServer, CatalogSource, Runner } from '$lib/types';

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

	// ---- By-type facet filter -------------------------------------------------
	// Narrow the browse list to one or more package/registry types (npm, pypi, oci,
	// nuget, mcpb, remote). Empty selection = show all (default). The visible list is
	// a pure $derived of (servers, selectedTypes) — deterministic, no refetch.
	let selectedTypes = $state<string[]>([]);
	const availableTypes = $derived(
		[...new Set(servers.flatMap((s) => s.registry_types))].sort()
	);
	const filterActive = $derived(selectedTypes.length > 0);
	const visibleServers = $derived(
		filterActive
			? servers.filter((s) => s.registry_types.some((t) => selectedTypes.includes(t)))
			: servers
	);

	function toggleType(t: string) {
		selectedTypes = selectedTypes.includes(t)
			? selectedTypes.filter((x) => x !== t)
			: [...selectedTypes, t];
		autoLoads = 0; // a changed filter gets a fresh sparse-page budget
	}

	// Client-side filtering after cursor pagination can leave a page with few/zero
	// visible cards while more pages remain. Pull more pages until the grid has a
	// sensible minimum — bounded so it can never loop. Reset per query / filter change.
	const MIN_VISIBLE = 6;
	const MAX_AUTOLOADS = 5;
	let autoLoads = 0; // plain (non-reactive) guard so bumping it doesn't retrigger

	$effect(() => {
		if (
			filterActive &&
			nextCursor &&
			!loading &&
			!loadingMore &&
			visibleServers.length < MIN_VISIBLE &&
			autoLoads < MAX_AUTOLOADS
		) {
			autoLoads += 1;
			void loadMore();
		}
	});

	// A monotonically increasing token guards against out-of-order responses: a
	// stale in-flight query (slow network) can't clobber a newer one's results.
	let queryToken = 0;

	let searchTimer: ReturnType<typeof setTimeout> | undefined;

	$effect(() => {
		void load();
		return () => {
			clearTimeout(searchTimer);
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
		autoLoads = 0; // a fresh query gets a fresh sparse-page budget
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

	// A stable per-row key. The official list can carry several versions of one server
	// (same name/id), so the name alone isn't unique — include the version.
	function rowKey(s: CatalogServer): string {
		return `${s.source}:${s.id}@${s.version ?? ''}`;
	}

	// One key per server (not per version) for the version picker state.
	function serverKey(s: CatalogServer): string {
		return `${s.source}:${s.id}`;
	}

	// ---- Version picker -------------------------------------------------------
	// The list is deduped to each server's latest version; the dropdown lazily loads
	// the full version list (on focus) so the operator can pick an older one. Until
	// then it shows just the latest, and Install defaults to it.
	let versionsByKey = $state<Record<string, string[]>>({});
	let chosenVersion = $state<Record<string, string>>({});
	let versionsLoading = $state<Record<string, boolean>>({});

	async function loadVersions(server: CatalogServer) {
		const key = serverKey(server);
		if (!server.version || versionsLoading[key]) return;
		// Skip if the cache is already fresh for this card's current latest (the list is
		// latest-first, so cache[0] is the latest). This also stops empty/failed fetches
		// from retrying, yet still refetches when the card later returns a newer latest.
		const cached = versionsByKey[key];
		if (cached && cached[0] === server.version) return;
		versionsLoading[key] = true;
		try {
			const res = await getCatalogVersions(server.id, server.source);
			versionsByKey[key] = res.versions.length ? res.versions : [server.version];
			// If a previously chosen version is no longer offered, fall back to latest.
			if (chosenVersion[key] && !versionsByKey[key].includes(chosenVersion[key])) {
				chosenVersion[key] = server.version;
			}
		} catch {
			versionsByKey[key] = [server.version];
		} finally {
			versionsLoading[key] = false;
		}
	}

	function onSearchInput() {
		// Invalidate any in-flight pagination immediately: bumping the token now means a
		// Load-more click before the debounce fires can't append a stale page.
		queryToken++;
		clearTimeout(searchTimer);
		searchTimer = setTimeout(runSearch, 300);
	}

	function selectSource(id: string) {
		if (id === source) return;
		source = id;
		nextCursor = null;
		selectedTypes = []; // types differ per source; start unfiltered
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
			// Drop any server already shown: the registry paginates by name:version, so a
			// server's versions can straddle a page boundary even after server-side dedup.
			const seen = new Set(servers.map((s) => `${s.source}:${s.id}`));
			const fresh = res.servers.filter((s) => !seen.has(`${s.source}:${s.id}`));
			servers = [...servers, ...fresh];
			nextCursor = res.next_cursor;
		} catch (err) {
			flashToast(errorMessage(err), 'error');
		} finally {
			loadingMore = false;
		}
	}

	// Resolve the chosen server's drafts, stash a PendingInstall, and route to the
	// review form. Prefers the first auto-installable package; otherwise hands off a
	// manual scaffold, carrying the reason / remote endpoints so the form isn't blank.
	async function install(server: CatalogServer) {
		if (installing) return;
		installing = rowKey(server);
		try {
			// Resolve the version the operator picked in the dropdown (defaults to the
			// latest shown on the card), not a blanket "latest".
			const version = chosenVersion[serverKey(server)] ?? server.version ?? 'latest';
			const detail = await getCatalogServer(server.id, server.source, version);
			if (detail.server.status === 'deleted') {
				// Removed from the registry for moderation — don't hand over an install form.
				flashToast('This server was removed from the registry and can’t be installed.', 'error');
				return;
			}
			const installableDraft = detail.drafts.find((d) => d.installable);
			const draft = installableDraft ?? detail.drafts[0] ?? null;
			const supportMeta = sources.find((s) => s.id === server.source);
			const versionTag = detail.server.version ? `@${detail.server.version}` : '';

			// Prefer a local package install; otherwise, if the server exposes a remote
			// (HTTP/SSE) endpoint we can actually proxy, install it as a remote server.
			// Pick the first endpoint whose transport is supported (not just remotes[0],
			// which could be an unsupported type), and canonicalize any alias.
			const remote = detail.remotes.find((r) => r.url && canonicalRemoteTransport(r.type));
			const remoteTransport = remote ? canonicalRemoteTransport(remote.type) : null;
			if (!installableDraft && remote && remoteTransport) {
				setPendingInstall({
					initial: {
						name: detail.server.title || detail.server.name,
						runner: 'remote',
						command: remote.url,
						args: [remoteTransport],
						// Scaffold the endpoint's declared headers (required ones prefilled
						// empty) so the form prompts for upstream auth instead of silently
						// dropping it.
						env: remote.headers ?? {},
						// Don't auto-start: required headers / a templated URL may need
						// filling first. Review, then enable.
						enabled: false
					},
					source: `catalog:${detail.server.name}${versionTag}`,
					sourceLabel: supportMeta?.label ?? server.source,
					installSupport: 'auto',
					// Carry header/URL-template warnings so required upstream auth isn't lost.
					warnings: remote.warnings ?? [],
					notes: [...detail.notes, `Proxying remote ${remote.type} endpoint: ${remote.url}`],
					repositoryUrl: detail.server.repository_url,
					webUrl: detail.server.web_url
				});
				await goto('/catalog/install');
				return;
			}

			const autoInstallable = !!draft?.installable;

			// Notes shown above the form: source notes, plus (when we couldn't derive a
			// runnable command) why, and any remote endpoints — so a manual/empty form is
			// always explained rather than mysteriously blank.
			const notes = [...detail.notes];
			if (!autoInstallable && draft?.reason) notes.push(draft.reason);
			for (const r of detail.remotes) {
				notes.push(`Remote ${r.type} endpoint (not a local runner): ${r.url}`);
			}

			const warnings = draft?.warnings ?? [];
			setPendingInstall({
				initial: {
					name: detail.server.title || detail.server.name,
					runner: (draft?.runner ?? 'npx') as Runner,
					command: draft?.command ?? '',
					args: draft?.args ?? [],
					env: draft?.env ?? {},
					// Only auto-start a clean, fully-resolved install; anything with
					// required/secret/placeholder values or no runnable command must be
					// reviewed first, so don't boot a broken server.
					enabled: autoInstallable && warnings.length === 0
				},
				source: `catalog:${detail.server.name}${versionTag}`,
				sourceLabel: supportMeta?.label ?? server.source,
				installSupport: detail.manual_install || !autoInstallable ? 'manual' : 'auto',
				warnings,
				notes,
				repositoryUrl: detail.server.repository_url,
				webUrl: detail.server.web_url
			});
			await goto('/catalog/install');
		} catch (err) {
			flashToast(errorMessage(err), 'error');
		} finally {
			// Always clear the spinner: on a successful nav the component unmounts
			// (harmless); on a cancelled/aborted nav we'd otherwise stay stuck.
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
		<!-- Type facet: filter by package/registry type (npm, pypi, remote, …). -->
		{#if availableTypes.length > 1}
			<div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by type">
				<span class="mr-0.5 text-xs text-[var(--color-ink-dim)]">Type</span>
				{#each availableTypes as t (t)}
					{@const on = selectedTypes.includes(t)}
					<button
						type="button"
						onclick={() => toggleType(t)}
						aria-pressed={on}
						class="rounded-full border px-2.5 py-0.5 font-mono text-[11px] transition"
						style={on
							? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 12%, transparent); color: var(--color-accent);'
							: 'border-color: var(--color-line); color: var(--color-ink-muted);'}
					>
						{t}
					</button>
				{/each}
				{#if filterActive}
					<button
						type="button"
						onclick={() => (selectedTypes = [])}
						class="ml-1 text-[11px] text-[var(--color-ink-dim)] underline decoration-dotted underline-offset-2 transition hover:text-[var(--color-ink)]"
					>
						Clear
					</button>
				{/if}
			</div>
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
	{:else if visibleServers.length === 0}
		<div class="flex flex-col items-center gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-10 text-center">
			{#if filterActive && servers.length > 0}
				<p class="text-sm text-[var(--color-ink-muted)]">
					No <span class="font-mono">{selectedTypes.join(', ')}</span> servers on the loaded
					pages — {servers.length} result{servers.length === 1 ? '' : 's'} of other types are
					hidden{nextCursor ? ', and more pages remain' : ''}.
				</p>
				<div class="flex flex-wrap items-center justify-center gap-2">
					<button
						type="button"
						onclick={() => (selectedTypes = [])}
						class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)]"
					>
						Clear filter
					</button>
					{#if nextCursor}
						<!-- Matches may be further in the catalog; keep pagination reachable even
						     when the current pages filtered down to nothing. -->
						<button
							type="button"
							onclick={loadMore}
							disabled={loadingMore}
							class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)] disabled:opacity-60"
						>
							{loadingMore ? 'Loading…' : 'Load more'}
						</button>
					{/if}
				</div>
			{:else}
				<p class="text-sm text-[var(--color-ink-muted)]">No servers match your search.</p>
			{/if}
		</div>
	{:else}
		<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each visibleServers as server (rowKey(server))}
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
								{:else if rt === 'remote'}
									<RunnerBadge runner="remote" />
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
								<!-- Version picker: lazily loads all versions on focus; defaults to latest. -->
								<div class="relative inline-flex items-center">
									<select
										aria-label="Version for {server.name}"
										value={chosenVersion[serverKey(server)] ?? server.version}
										onmouseenter={() => loadVersions(server)}
										onfocus={() => loadVersions(server)}
										onpointerdown={() => loadVersions(server)}
										onchange={(e) => (chosenVersion[serverKey(server)] = e.currentTarget.value)}
										class="cursor-pointer appearance-none rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] py-0.5 pl-2 pr-6 font-mono text-[11px] text-[var(--color-ink-muted)] outline-none transition hover:border-[var(--color-line-strong)] focus:border-[var(--color-line-strong)]"
									>
										{#each versionsByKey[serverKey(server)] ?? [server.version] as v (v)}
											<option value={v}>v{v}</option>
										{/each}
									</select>
									<svg class="pointer-events-none absolute right-1.5 size-3 text-[var(--color-ink-dim)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
										<path d="m6 9 6 6 6-6" />
									</svg>
								</div>
							{/if}
							{#if server.status && server.status !== 'active'}
								<span style="color: {statusTone(server.status)}">{server.status}</span>
							{/if}
						</div>
						<button
							type="button"
							onclick={() => install(server)}
							disabled={installing === rowKey(server)}
							class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition active:translate-y-px disabled:opacity-60"
							style="background-color: var(--color-accent); color: var(--color-accent-ink);"
						>
							{#if installing === rowKey(server)}
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

