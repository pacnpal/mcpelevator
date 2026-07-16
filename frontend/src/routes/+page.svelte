<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { listServers } from '$lib/api';
	import { hasActiveStartup, pollingInterval } from '$lib/startup';
	import type { ServerSummary } from '$lib/types';
	import ServerCard from '$lib/components/ServerCard.svelte';
	import Toast from '$lib/components/Toast.svelte';

	type LoadState = 'loading' | 'ready' | 'error';

	let servers = $state<ServerSummary[]>([]);
	let loadState = $state<LoadState>('loading');
	let toast = $state<string | null>(null);
	let toastTimer: ReturnType<typeof setTimeout> | undefined;

	const runningCount = $derived(
		servers.filter((s) => !hasActiveStartup(s) && s.state === 'running').length
	);

	function flashToast(message: string) {
		toast = message;
		clearTimeout(toastTimer);
		toastTimer = setTimeout(() => (toast = null), 6000);
	}

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
		pollingStopped = false;
		void load().finally(schedulePoll);
		return () => {
			pollingStopped = true;
			clearTimeout(pollTimer);
			clearTimeout(toastTimer);
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
		void goto('/', { replaceState: true, noScroll: true, keepFocus: true });
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
		<!-- Server grid -->
		<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each servers as server (server.id)}
				<ServerCard
					{server}
					onchange={applyUpdate}
					ondelete={removeServer}
					onerror={flashToast}
				/>
			{/each}
		</div>
	{/if}
</section>

<!-- Non-blocking toast -->
{#if toast}
	<div
		class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:justify-end sm:px-6"
	>
		<div class="w-full max-w-sm">
			<Toast message={toast} onclose={() => (toast = null)} />
		</div>
	</div>
{/if}
