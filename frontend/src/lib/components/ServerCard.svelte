<script lang="ts">
	import { disableServer, enableServer } from '$lib/api';
	import type { ServerSummary } from '$lib/types';
	import CopyButton from './CopyButton.svelte';
	import RunnerBadge from './RunnerBadge.svelte';
	import StatePill from './StatePill.svelte';

	let {
		server,
		onchange,
		onerror
	}: {
		server: ServerSummary;
		/** Called with the updated summary after a successful toggle. */
		onchange?: (next: ServerSummary) => void;
		/** Called with a human-readable message if the toggle fails. */
		onerror?: (message: string) => void;
	} = $props();

	let busy = $state(false);

	// The action available depends on desired-state (`enabled`), not live state:
	// an enabled-but-stopped server should still offer "Stop" to clear intent.
	const wantsRun = $derived(server.enabled);
	const transient = $derived(
		server.state === 'starting' || server.state === 'stopping'
	);

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
</script>

<article
	class="group flex flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5 transition-colors hover:border-[var(--color-line-strong)]"
>
	<!-- Header: identity + state -->
	<header class="flex items-start justify-between gap-3">
		<div class="min-w-0">
			<h3 class="truncate text-[15px] font-semibold text-[var(--color-ink)]">
				{server.name}
			</h3>
			<p class="mt-0.5 truncate font-mono text-xs text-[var(--color-ink-dim)]">
				{server.slug}
			</p>
		</div>
		<StatePill state={server.state} />
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
		<span
			class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium transition-colors"
			class:opacity-40={!server.transports.rest_openapi}
			style={server.transports.rest_openapi
				? 'color: var(--color-accent); border-color: color-mix(in oklab, var(--color-accent) 30%, transparent); background-color: color-mix(in oklab, var(--color-accent) 10%, transparent);'
				: 'color: var(--color-ink-dim); border-color: var(--color-line);'}
		>
			REST
		</span>
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
		<div class="flex flex-wrap items-center gap-1.5">
			<CopyButton value={server.urls.mcp} label="MCP" />
			<CopyButton value={server.urls.rest} label="REST" />
		</div>

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
				<svg
					class="size-3.5 animate-spin"
					viewBox="0 0 24 24"
					fill="none"
					aria-hidden="true"
				>
					<circle
						cx="12"
						cy="12"
						r="9"
						stroke="currentColor"
						stroke-width="2.5"
						stroke-opacity="0.25"
					/>
					<path
						d="M21 12a9 9 0 0 0-9-9"
						stroke="currentColor"
						stroke-width="2.5"
						stroke-linecap="round"
					/>
				</svg>
				{transient ? '…' : wantsRun ? 'Stopping' : 'Starting'}
			{:else if wantsRun}
				<svg
					class="size-3.5"
					viewBox="0 0 24 24"
					fill="currentColor"
					aria-hidden="true"
				>
					<rect x="7" y="7" width="10" height="10" rx="1.5" />
				</svg>
				Stop
			{:else}
				<svg
					class="size-3.5"
					viewBox="0 0 24 24"
					fill="currentColor"
					aria-hidden="true"
				>
					<path d="M8 5v14l11-7z" />
				</svg>
				Start
			{/if}
		</button>
	</footer>
</article>
