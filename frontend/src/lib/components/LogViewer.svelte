<script lang="ts">
	import { streamLogs } from '$lib/api';
	import type { ServerState } from '$lib/types';

	let { serverId, serverState }: { serverId: string; serverState: ServerState } = $props();

	const MAX_LINES = 1000;
	// Strip terminal color codes (fastmcp/uvicorn log in ANSI) for a clean pane.
	const ANSI = /\x1b\[[0-9;]*m/g;

	type Conn = 'idle' | 'connecting' | 'live' | 'reconnecting';

	let lines = $state<string[]>([]);
	let conn = $state<Conn>('idle');
	let notRunning = $state(false);
	let stuckToBottom = $state(true);
	let pane = $state<HTMLDivElement | undefined>(undefined);

	// Only open the stream when a process likely exists (avoids reconnect storms
	// against a stopped server).
	const shouldConnect = $derived(serverState !== 'stopped');

	const dot = $derived.by(() => {
		if (notRunning || conn === 'idle')
			return { color: 'var(--color-state-stopped)', pulse: false, label: 'idle' };
		if (conn === 'live') return { color: 'var(--color-state-running)', pulse: false, label: 'live' };
		return { color: 'var(--color-state-starting)', pulse: true, label: 'connecting' };
	});

	function onScroll() {
		if (!pane) return;
		stuckToBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 24;
	}

	// Stream lifecycle — reopens when serverId or the should-connect gate changes.
	// Fetch + ReadableStream (not EventSource) so the Authorization header is sent.
	// Reconnect is manual: a dropped stream retries after a short backoff until the
	// effect is torn down (serverId change / unmount).
	$effect(() => {
		if (!shouldConnect) {
			conn = 'idle';
			return;
		}
		notRunning = false;
		conn = 'connecting';
		const ctrl = new AbortController();
		let stopped = false;

		void (async () => {
			while (!stopped && !ctrl.signal.aborted) {
				try {
					await streamLogs(
						serverId,
						{
							onOpen: () => (conn = 'live'),
							onLine: (line) => {
								lines.push(line.replace(ANSI, ''));
								if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
							},
							onInfo: () => {
								// Server isn't running — stop instead of reconnect-looping.
								notRunning = true;
								conn = 'idle';
								stopped = true;
							}
						},
						ctrl.signal
					);
				} catch {
					if (stopped || ctrl.signal.aborted) return;
					conn = 'reconnecting';
				}
				if (stopped || ctrl.signal.aborted) return;
				await new Promise((r) => setTimeout(r, 1000)); // backoff before re-opening
			}
		})();

		return () => {
			stopped = true;
			ctrl.abort();
		};
	});

	// Auto-scroll to the newest line unless the user has scrolled up.
	$effect(() => {
		void lines.length;
		if (pane && stuckToBottom) pane.scrollTop = pane.scrollHeight;
	});
</script>

<div
	class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
>
	<div class="flex items-center justify-between gap-3">
		<div class="flex items-center gap-2">
			<h2 class="text-sm font-semibold text-[var(--color-ink)]">Logs</h2>
			<span class="inline-flex items-center gap-1.5 text-xs text-[var(--color-ink-dim)]">
				<span
					class="size-1.5 rounded-full"
					class:animate-pulse-dot={dot.pulse}
					style="background-color: {dot.color};"
				></span>
				{dot.label}
			</span>
		</div>
		<button
			type="button"
			onclick={() => (lines = [])}
			disabled={lines.length === 0}
			class="rounded-md border border-[var(--color-line)] px-2.5 py-1 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-40"
		>
			Clear
		</button>
	</div>

	<div
		bind:this={pane}
		onscroll={onScroll}
		class="h-72 overflow-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] p-3 font-mono text-xs leading-relaxed"
	>
		{#if notRunning}
			<p class="text-[var(--color-ink-dim)]">Server isn't running — no live logs.</p>
		{:else if lines.length === 0}
			<p class="text-[var(--color-ink-dim)]">Waiting for output…</p>
		{:else}
			{#each lines as line}
				<div class="whitespace-pre-wrap break-all text-[var(--color-ink-muted)]">{line}</div>
			{/each}
		{/if}
	</div>
</div>
