<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { getAuthStatus, getInstanceUsage, listServers } from '#lib/api.js';
	import {
		PROBLEM_STATES,
		RUNNER_LABEL,
		RUNNERS,
		STATE_FILTERS,
		filterServers,
		filterState,
		isFiltered,
		ownerOptions,
		parseQuery,
		serializeQuery,
		sortServers,
		type ServerQuery,
		type SortKey,
		type StateFilter
	} from '#lib/serverFilter.js';
	import { hasActiveStartup, pollingInterval } from '#lib/startup.js';
	import type { AuthUser, Runner, ServerSummary } from '#lib/types.js';
	import ServerCard from '#lib/components/ServerCard.svelte';
	import { flashToast } from '#lib/toast.svelte.js';

	type LoadState = 'loading' | 'ready' | 'error';

	let servers = $state<ServerSummary[]>([]);
	let loadState = $state<LoadState>('loading');

	// Search, filter and sort live in the URL query, so a filtered view can be
	// linked and survives a reload. The search box keeps its own copy so a slow
	// navigation can't rewrite what's being typed.
	const query = $derived(parseQuery(page.url.searchParams));
	let search = $state(parseQuery(page.url.searchParams).q);
	let me = $state<AuthUser | null>(null);
	const isAdmin = $derived(me?.role === 'admin');
	// Offered to admins only, and only when there's more than one owner to pick.
	const owners = $derived(isAdmin ? ownerOptions(servers, me?.id ?? null) : []);
	// Members only see their own servers, and an owner that isn't on offer is ignored.
	const owner = $derived(owners.some((o) => o.value === query.owner) ? query.owner : '');
	const effective = $derived<ServerQuery>({ ...query, q: search, owner });
	const filtering = $derived(isFiltered(effective));

	// "Most used" needs the instance usage rollup: one request, made only when picked.
	let calls = $state<Record<string, number> | null>(null);
	$effect(() => {
		if (query.sort !== 'used' || calls !== null) return;
		calls = {};
		getInstanceUsage(7)
			.then((u) => (calls = Object.fromEntries(u.servers.map((r) => [r.server_id, r.tool_calls]))))
			.catch(() => flashToast('Could not load usage, so most used falls back to name.'));
	});

	const visible = $derived(
		sortServers(filterServers(servers, effective, me?.id ?? null), query.sort, calls ?? {})
	);
	// Each state's count under every other filter, so a chip says what picking it shows.
	const stateCounts = $derived.by(() => {
		const counts = Object.fromEntries(STATE_FILTERS.map((st) => [st, 0])) as Record<
			StateFilter,
			number
		>;
		for (const s of filterServers(servers, { ...effective, states: [] }, me?.id ?? null)) {
			counts[filterState(s)] += 1;
		}
		return counts;
	});
	const runnerChoices = $derived(
		RUNNERS.filter((r) => query.runners.includes(r) || servers.some((s) => s.runner === r))
	);
	const problemsOn = $derived(PROBLEM_STATES.every((st) => query.states.includes(st)));

	const STATE_LABEL: Record<StateFilter, string> = {
		running: 'Running',
		idle: 'Idle',
		starting: 'Starting',
		unhealthy: 'Unhealthy',
		failed: 'Failed',
		stopped: 'Stopped'
	};
	const SORT_LABEL: Record<SortKey, string> = {
		name: 'Name',
		state: 'State, problems first',
		tools: 'Tool count',
		added: 'Recently added',
		used: 'Most used (7 days)'
	};

	function setQuery(next: Partial<ServerQuery>) {
		const url = `${page.url.pathname}${serializeQuery({ ...query, q: search, ...next })}`;
		// SvelteKit 3: `replace` is the old replaceState, `reset: false` keeps focus and scroll.
		void goto(url, { replace: true, reset: false });
	}

	function toggle<T>(list: T[], items: readonly T[], on: boolean): T[] {
		const rest = list.filter((v) => !items.includes(v));
		return on ? rest : [...rest, ...items];
	}

	function clearFilters() {
		search = '';
		setQuery({ q: '', states: [], runners: [], owner: '' });
	}

	const runningCount = $derived(
		servers.filter((s) => !hasActiveStartup(s) && s.state === 'running').length
	);

	let loadInFlight = false;
	let mutationRevision = 0;
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let pollingStopped = false;

	async function load(silent = false) {
		if (loadInFlight) return;
		loadInFlight = true;
		const revision = mutationRevision;
		if (!silent) loadState = 'loading';
		try {
			const result = await listServers();
			if (revision !== mutationRevision) return;
			servers = result;
			loadState = 'ready';
		} catch (err) {
			if (!silent) {
				loadState = 'error';
				const detail = err instanceof Error ? err.message : 'Unknown error';
				flashToast(`Could not reach the backend — ${detail}`);
			}
		} finally {
			loadInFlight = false;
		}
	}

	function schedulePoll() {
		clearTimeout(pollTimer);
		if (pollingStopped) return;
		pollTimer = setTimeout(async () => {
			await load(true);
			schedulePoll();
		}, pollingInterval(servers));
	}

	// Replace a single server in-place after a toggle returns its fresh state.
	function applyUpdate(next: ServerSummary) {
		mutationRevision += 1;
		servers = servers.map((s) => (s.id === next.id ? next : s));
		schedulePoll();
	}

	// Drop a server from the list after it's deleted from a card's menu.
	function removeServer(id: string) {
		mutationRevision += 1;
		const removed = servers.find((s) => s.id === id);
		servers = servers.filter((s) => s.id !== id);
		flashToast(removed ? `Deleted ${removed.name}` : 'Server deleted');
		schedulePoll();
	}

	$effect(() => {
		getAuthStatus()
			.then((status) => (me = status.user))
			.catch(() => undefined);
	});

	$effect(() => {
		pollingStopped = false;
		void load().finally(schedulePoll);
		return () => {
			pollingStopped = true;
			clearTimeout(pollTimer);
		};
	});

	// The OAuth callback's failure redirect lands HERE (`/?oauth=error` — a fixed,
	// literal target; see backend/app/api/auth.py). The popup flow consumes it via the
	// root layout before this page ever shows, but the full-tab fallback (popup blocked,
	// or a cross-origin callback) arrives as a regular navigation — surface the failure
	// instead of silently dumping the operator on the server list, then strip the query
	// so a refresh doesn't re-toast.
	$effect(() => {
		void page.url.search;
		if (page.url.searchParams.get('oauth') !== 'error') return;
		const reason = page.url.searchParams.get('reason');
		flashToast(reason ? `OAuth failed: ${reason}` : 'OAuth sign-in failed.');
		void goto('/', { replaceState: true, reset: false });
	});
</script>

<section class="flex flex-col gap-6">
	<!-- Page heading -->
	<div class="flex flex-wrap items-end justify-between gap-4">
		<div>
			<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">
				Servers
			</h1>
			<p class="mt-1 text-sm text-[var(--color-ink-muted)]">
				{#if loadState === 'ready' && servers.length > 0}
					{runningCount} of {servers.length} running
				{:else}
					Manage and elevate your MCP servers
				{/if}
			</p>
		</div>

		<div class="flex items-center gap-2">
			<button
				type="button"
				onclick={() => load()}
				class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition active:translate-y-px hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
			>
				<svg
					class="size-4"
					class:animate-spin={loadState === 'loading'}
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
					<path d="M21 3v5h-5" />
				</svg>
				<span class="hidden sm:inline">Refresh</span>
			</button>
			<a
				href="/add"
				class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-2 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)]"
			>
				<svg
					class="size-4"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2.5"
					stroke-linecap="round"
					aria-hidden="true"
				>
					<path d="M12 5v14M5 12h14" />
				</svg>
				Add server
			</a>
		</div>
	</div>

	<!-- Content states -->
	{#if loadState === 'loading'}
		<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each Array(6) as _, i (i)}
				<div
					class="h-52 animate-pulse rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)]"
				></div>
			{/each}
		</div>
	{:else if loadState === 'error'}
		<div
			class="flex flex-col items-center justify-center gap-4 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] px-6 py-16 text-center"
		>
			<div
				class="flex size-12 items-center justify-center rounded-full border border-[color-mix(in_oklab,var(--color-state-failed)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-state-failed)_12%,transparent)]"
			>
				<svg
					class="size-5 text-[var(--color-state-failed)]"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M12 9v4M12 17h.01" />
					<path
						d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"
					/>
				</svg>
			</div>
			<div class="space-y-1">
				<p class="text-base font-semibold text-[var(--color-ink)]">
					Backend unreachable
				</p>
				<p class="mx-auto max-w-sm text-sm text-[var(--color-ink-muted)]">
					mcpelevator couldn't load your servers. The backend may not be running
					yet — start it and try again.
				</p>
			</div>
			<button
				type="button"
				onclick={() => load()}
				class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink)] transition active:translate-y-px hover:border-[var(--color-line-strong)]"
			>
				Retry
			</button>
		</div>
	{:else if servers.length === 0}
		<!-- Empty state -->
		<div
			class="flex flex-col items-center justify-center gap-5 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] bg-[var(--color-surface)] px-6 py-20 text-center"
		>
			<div
				class="flex size-14 items-center justify-center rounded-2xl border border-[var(--color-line-strong)] bg-[var(--color-surface-2)]"
			>
				<svg class="size-7" viewBox="0 0 24 24" fill="none" aria-hidden="true">
					<path d="M12 3 6 10h12z" fill="var(--color-accent)" />
					<rect
						x="5"
						y="13"
						width="14"
						height="2.5"
						rx="1.25"
						fill="var(--color-ink-muted)"
					/>
					<rect
						x="8"
						y="18"
						width="8"
						height="2.5"
						rx="1.25"
						fill="var(--color-ink-dim)"
					/>
				</svg>
			</div>
			<div class="space-y-2">
				<h2 class="text-lg font-semibold text-[var(--color-ink)]">
					No servers yet — elevate your first MCP server
				</h2>
				<p class="mx-auto max-w-md text-sm text-[var(--color-ink-muted)]">
					Add an MCP server and mcpelevator will run it for you, exposing it over
					MCP and REST with health monitoring built in.
				</p>
			</div>
			<div class="flex flex-col items-center gap-3 sm:flex-row">
				<a
					href="/catalog"
					class="inline-flex items-center gap-2 rounded-lg bg-[var(--color-accent)] px-5 py-2.5 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)]"
				>
					<svg
						class="size-4"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2.5"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<circle cx="11" cy="11" r="8" />
						<path d="m21 21-4.3-4.3" />
					</svg>
					Browse the registry
				</a>
				<a
					href="/add"
					class="inline-flex items-center gap-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-5 py-2.5 text-sm font-semibold text-[var(--color-ink)] transition active:translate-y-px hover:border-[var(--color-line-strong)]"
				>
					<svg
						class="size-4"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2.5"
						stroke-linecap="round"
						aria-hidden="true"
					>
						<path d="M12 5v14M5 12h14" />
					</svg>
					Add manually
				</a>
			</div>
		</div>
	{:else}
		<!-- Search, filter and sort -->
		<div class="flex flex-col gap-3">
			<div class="flex flex-col gap-2 sm:flex-row">
				<label for="server-search" class="sr-only">Search servers</label>
				<input
					id="server-search"
					type="search"
					value={search}
					oninput={(e) => {
						search = e.currentTarget.value;
						setQuery({ q: search });
					}}
					placeholder="Search name, slug, runner or tool"
					autocomplete="off"
					spellcheck="false"
					class="min-w-0 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm text-[var(--color-ink)] outline-hidden transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
				/>
				<div class="flex gap-2">
					{#if owners.length > 1}
						<label for="server-owner" class="sr-only">Owner</label>
						<select
							id="server-owner"
							value={owner}
							onchange={(e) => setQuery({ owner: e.currentTarget.value })}
							class="min-w-0 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm text-[var(--color-ink)] outline-hidden transition focus:border-[var(--color-line-strong)] sm:flex-none"
						>
							<option value="">Any owner</option>
							{#each owners as o (o.value)}
								<option value={o.value}>{o.label}</option>
							{/each}
						</select>
					{/if}
					<label for="server-sort" class="sr-only">Sort by</label>
					<select
						id="server-sort"
						value={query.sort}
						onchange={(e) => setQuery({ sort: e.currentTarget.value as SortKey })}
						class="min-w-0 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm text-[var(--color-ink)] outline-hidden transition focus:border-[var(--color-line-strong)] sm:flex-none"
					>
						{#each Object.entries(SORT_LABEL) as [value, label] (value)}
							<option {value}>Sort: {label}</option>
						{/each}
					</select>
				</div>
			</div>

			<div role="group" aria-label="Filter by state" class="flex flex-wrap gap-1.5">
				<button
					type="button"
					aria-pressed={problemsOn}
					onclick={() => setQuery({ states: toggle(query.states, PROBLEM_STATES, problemsOn) })}
					class="nav-item h-8 px-2.5 text-xs"
				>
					Problems
					<span class="tabular-nums text-[var(--color-ink-dim)]"
						>{stateCounts.failed + stateCounts.unhealthy}</span
					>
				</button>
				{#each STATE_FILTERS as st (st)}
					{@const on = query.states.includes(st)}
					<button
						type="button"
						aria-pressed={on}
						onclick={() => setQuery({ states: toggle(query.states, [st], on) })}
						class="nav-item h-8 px-2.5 text-xs"
					>
						<span
							class="size-1.5 rounded-full"
							style="background-color: var(--color-state-{st});"
							aria-hidden="true"
						></span>
						{STATE_LABEL[st]}
						<span class="tabular-nums text-[var(--color-ink-dim)]">{stateCounts[st]}</span>
					</button>
				{/each}
			</div>

			{#if runnerChoices.length > 1}
				<div role="group" aria-label="Filter by runner" class="flex flex-wrap gap-1.5">
					{#each runnerChoices as r (r)}
						{@const on = query.runners.includes(r)}
						<button
							type="button"
							aria-pressed={on}
							onclick={() => setQuery({ runners: toggle<Runner>(query.runners, [r], on) })}
							class="nav-item h-8 px-2.5 font-mono text-[11px]"
						>
							{RUNNER_LABEL[r]}
						</button>
					{/each}
				</div>
			{/if}

			<div
				class="flex min-h-8 items-center gap-3 text-xs text-[var(--color-ink-muted)]"
				aria-live="polite"
			>
				{#if filtering}
					<span>{visible.length} of {servers.length} servers</span>
					<button
						type="button"
						onclick={clearFilters}
						class="rounded-md px-1.5 py-1 font-medium text-[var(--color-ink-muted)] underline-offset-2 transition hover:text-[var(--color-ink)] hover:underline"
					>
						Clear filters
					</button>
				{/if}
			</div>
		</div>

		<!-- Server grid -->
		<h2 class="sr-only">{filtering ? 'Matching servers' : 'All servers'}</h2>
		{#if visible.length === 0}
			<div
				class="flex flex-col items-center justify-center gap-4 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] px-6 py-16 text-center"
			>
				<p class="text-sm text-[var(--color-ink-muted)]">No servers match these filters.</p>
				<button
					type="button"
					onclick={clearFilters}
					class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink)] transition active:translate-y-px hover:border-[var(--color-line-strong)]"
				>
					Clear filters
				</button>
			</div>
		{:else}
			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
				{#each visible as server (server.id)}
					<ServerCard
						{server}
						onchange={applyUpdate}
						ondelete={removeServer}
						onerror={flashToast}
					/>
				{/each}
			</div>
		{/if}
	{/if}
</section>
