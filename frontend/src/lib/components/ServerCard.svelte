<script lang="ts">
	import { goto } from '$app/navigation';

	import { cloneServer, deleteServer, disableServer, enableServer, errorMessage } from '$lib/api';
	import type { ServerSummary } from '$lib/types';
	import CopyMenu from './CopyMenu.svelte';
	import RunnerBadge from './RunnerBadge.svelte';
	import StatePill from './StatePill.svelte';

	let {
		server,
		onchange,
		ondelete,
		onerror
	}: {
		server: ServerSummary;
		/** Called with the updated summary after a successful toggle. */
		onchange?: (next: ServerSummary) => void;
		/** Called with the deleted server's id after a successful delete. */
		ondelete?: (id: string) => void;
		/** Called with a human-readable message if an action fails. */
		onerror?: (message: string) => void;
	} = $props();

	let busy = $state(false);
	let menuOpen = $state(false);
	let confirmDelete = $state(false);
	let deleting = $state(false);
	let cloning = $state(false);
	let cardEl = $state<HTMLElement>();

	// The action available depends on desired-state (`enabled`), not live state:
	// an enabled-but-stopped server should still offer "Stop" to clear intent.
	const wantsRun = $derived(server.enabled);
	const transient = $derived(
		server.state === 'starting' || server.state === 'stopping'
	);

	const detailHref = $derived(`/server/${server.id}`);

	async function toggle() {
		if (busy) return;
		busy = true;
		try {
			const next = wantsRun
				? await disableServer(server.id)
				: await enableServer(server.id);
			onchange?.(next);
		} catch (err) {
			const message =
				err instanceof Error ? err.message : 'Failed to update server';
			onerror?.(message);
		} finally {
			busy = false;
		}
	}

	function closeMenu() {
		menuOpen = false;
		confirmDelete = false;
	}

	// Close the kebab menu on an outside pointer/escape. We attach native
	// listeners imperatively (not via svelte:window) so Svelte's event
	// delegation can't reorder them against the toggle click, and we listen on
	// `pointerdown` so selecting a menu item still fires its own click first.
	$effect(() => {
		if (!menuOpen) return;
		const onPointer = (e: PointerEvent) => {
			if (e.target instanceof Node && !cardEl?.contains(e.target)) closeMenu();
		};
		const onKey = (e: KeyboardEvent) => {
			if (e.key === 'Escape') closeMenu();
		};
		// Defer attaching until after the opening click has fully settled.
		const id = setTimeout(() => {
			document.addEventListener('pointerdown', onPointer);
			document.addEventListener('keydown', onKey);
		}, 0);
		return () => {
			clearTimeout(id);
			document.removeEventListener('pointerdown', onPointer);
			document.removeEventListener('keydown', onKey);
		};
	});

	async function doClone() {
		if (cloning) return;
		cloning = true;
		try {
			const copy = await cloneServer(server.id);
			closeMenu();
			// Land on the copy so it can be reviewed/edited, then enabled.
			await goto(`/server/${copy.id}`);
		} catch (err) {
			onerror?.(errorMessage(err));
		} finally {
			// goto resolves `false` (no throw) when a navigation is aborted, so reset
			// in finally — otherwise the Clone button could stay stuck disabled.
			cloning = false;
		}
	}

	async function doDelete() {
		if (deleting) return;
		deleting = true;
		try {
			await deleteServer(server.id);
			ondelete?.(server.id);
		} catch (err) {
			const message =
				err instanceof Error ? err.message : 'Failed to delete server';
			onerror?.(message);
			deleting = false;
			closeMenu();
		}
	}
</script>

{#snippet spinner(size: string)}
	<svg class="{size} animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
		<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
		<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
	</svg>
{/snippet}

<article
	bind:this={cardEl}
	class="group relative flex flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5 transition-colors hover:border-[var(--color-line-strong)]"
>
	<!-- Header: identity (link to detail) + state + menu -->
	<header class="flex items-start justify-between gap-3">
		<a
			href={detailHref}
			class="min-w-0 rounded-md outline-offset-4 transition-opacity hover:opacity-80"
		>
			<h3 class="truncate text-[15px] font-semibold text-[var(--color-ink)]">
				{server.name}
			</h3>
			<p class="mt-0.5 truncate font-mono text-xs text-[var(--color-ink-dim)]">
				{server.slug}
			</p>
		</a>
		<div class="flex shrink-0 items-center gap-1.5">
			<StatePill state={server.state} />
			<div class="relative">
				<button
					type="button"
					onclick={() => {
						menuOpen = !menuOpen;
						confirmDelete = false;
					}}
					aria-haspopup="menu"
					aria-expanded={menuOpen}
					aria-label="Server actions"
					class="rounded-md p-1 text-[var(--color-ink-dim)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
				>
					<svg class="size-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
						<circle cx="12" cy="5" r="1.6" />
						<circle cx="12" cy="12" r="1.6" />
						<circle cx="12" cy="19" r="1.6" />
					</svg>
				</button>

				{#if menuOpen}
					<div
						role="menu"
						class="absolute right-0 top-full z-20 mt-1 w-44 overflow-hidden rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-elevated)] py-1 shadow-2xl"
					>
						{#if !confirmDelete}
							<a
								href={detailHref}
								role="menuitem"
								class="flex items-center gap-2 px-3 py-2 text-sm text-[var(--color-ink-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
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
									<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
									<circle cx="12" cy="12" r="3" />
								</svg>
								View details
							</a>
							<a
								href={`${detailHref}/edit`}
								role="menuitem"
								class="flex items-center gap-2 px-3 py-2 text-sm text-[var(--color-ink-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
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
							<button
								type="button"
								role="menuitem"
								onclick={doClone}
								disabled={cloning}
								aria-busy={cloning}
								class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-[var(--color-ink-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70"
							>
								{#if cloning}
									{@render spinner('size-4')}
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
								{/if}
								Clone
							</button>
							<button
								type="button"
								role="menuitem"
								onclick={() => (confirmDelete = true)}
								class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition hover:bg-[color-mix(in_oklab,var(--color-state-failed)_12%,transparent)]"
								style="color: var(--color-state-failed);"
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
									<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
								</svg>
								Delete
							</button>
						{:else}
							<div class="flex flex-col gap-2 px-3 py-2.5">
								<p class="text-xs leading-snug text-[var(--color-ink)]">
									Delete <span class="font-semibold">{server.name}</span>? This stops
									and removes it.
								</p>
								<div class="flex items-center gap-1.5">
									<button
										type="button"
										onclick={doDelete}
										disabled={deleting}
										aria-busy={deleting}
										class="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-semibold text-white transition disabled:cursor-wait disabled:opacity-70"
										style="background-color: var(--color-state-failed);"
									>
										{#if deleting}
											{@render spinner('size-3.5')}
											Deleting
										{:else}
											Delete
										{/if}
									</button>
									<button
										type="button"
										onclick={() => (confirmDelete = false)}
										disabled={deleting}
										class="rounded-md border border-[var(--color-line)] px-2 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
									>
										Cancel
									</button>
								</div>
							</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	</header>

	<!-- Meta row: runner + transports -->
	<div class="flex flex-wrap items-center gap-1.5">
		<RunnerBadge runner={server.runner} />
		<span
			class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium transition-colors"
			class:opacity-40={!server.transports.mcp_http}
			style={server.transports.mcp_http
				? 'color: var(--color-accent); border-color: color-mix(in oklab, var(--color-accent) 30%, transparent); background-color: color-mix(in oklab, var(--color-accent) 10%, transparent);'
				: 'color: var(--color-ink-dim); border-color: var(--color-line);'}
		>
			MCP
		</span>
		<!-- REST/OpenAPI badge omitted: the surface isn't served yet (planned, M6). -->
	</div>

	<!-- Error surface -->
	{#if server.last_error}
		<p
			class="rounded-lg border border-[color-mix(in_oklab,var(--color-state-failed)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-state-failed)_10%,transparent)] px-3 py-2 font-mono text-[11px] leading-relaxed text-[var(--color-state-failed)]"
			title={server.last_error}
		>
			{server.last_error}
		</p>
	{/if}

	<!-- Footer: copy endpoints + start/stop -->
	<footer
		class="mt-auto flex items-center justify-between gap-2 border-t border-[var(--color-line)] pt-4"
	>
		<CopyMenu {server} />

		<button
			type="button"
			onclick={toggle}
			disabled={busy}
			aria-busy={busy}
			class="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
			class:running={wantsRun}
			style={wantsRun
				? 'color: var(--color-ink-muted); border: 1px solid var(--color-line);'
				: 'color: var(--color-accent-ink); background-color: var(--color-accent);'}
		>
			{#if busy}
				{@render spinner('size-3.5')}
				{transient ? '…' : wantsRun ? 'Stopping' : 'Starting'}
			{:else if wantsRun}
				<svg class="size-3.5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
					<rect x="7" y="7" width="10" height="10" rx="1.5" />
				</svg>
				Stop
			{:else}
				<svg class="size-3.5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
					<path d="M8 5v14l11-7z" />
				</svg>
				Start
			{/if}
		</button>
	</footer>
</article>
