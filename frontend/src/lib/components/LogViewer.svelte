<script lang="ts">
	import { ApiError, streamLogs } from '$lib/api';
	import type { ServerState, StartupStatus } from '$lib/types';

	let {
		serverId,
		serverState,
		startupStatus = null,
		compact = false
	}: {
		serverId: string;
		serverState: ServerState;
		startupStatus?: StartupStatus | null;
		compact?: boolean;
	} = $props();

	const MAX_LINES = 1000;
	const TRUNCATION_MARKER = 'omitted (buffer limit)';
	// Strip terminal color codes (fastmcp/uvicorn log in ANSI) for a clean pane.
	const ANSI = /\x1b\[[0-9;]*m/g;

	type Conn = 'idle' | 'connecting' | 'live' | 'reconnecting';

	let lines = $state<string[]>([]);
	let conn = $state<Conn>('idle');
	let notRunning = $state(false);
	let stuckToBottom = $state(true);
	let pane = $state<HTMLDivElement | undefined>(undefined);
	let seenServerId = '';
	let seenActivation: string | null = null;

	const startupActive = $derived(startupStatus !== null);
	const activationStartedAt = $derived(startupStatus?.activation_started_at ?? null);
	const resetKey = $derived(`${serverId}\u0000${activationStartedAt ?? ''}`);
	// Setup can produce logs before the bridge changes observed runtime state.
	const shouldConnect = $derived(startupActive || serverState !== 'stopped');

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

	$effect(() => {
		const currentServer = serverId;
		const currentActivation = activationStartedAt;
		if (
			currentServer !== seenServerId ||
			(currentActivation !== null && currentActivation !== seenActivation)
		) {
			lines = [];
			notRunning = false;
			stuckToBottom = true;
		}
		if (currentServer !== seenServerId) seenActivation = currentActivation;
		else if (currentActivation !== null) seenActivation = currentActivation;
		seenServerId = currentServer;
	});

	// Stream lifecycle reopens when the server, activation, or connection gate changes.
	// Fetch + ReadableStream (not EventSource) so the Authorization header is sent.
	// Reconnect is manual: a dropped stream retries after a short backoff until the
	// effect is torn down (serverId change / unmount).
	$effect(() => {
		void resetKey;
		const active = startupActive;
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
							onOpen: () => {
								// The endpoint replays its bounded activation backlog on every open.
								// Replace the previous copy before accepting replayed lines.
								lines = [];
								notRunning = false;
								conn = 'live';
							},
							onLine: (line) => {
								lines.push(line.replace(ANSI, ''));
								if (lines.length > MAX_LINES) {
									const removeAt = lines[0]?.includes(TRUNCATION_MARKER) ? 1 : 0;
									lines.splice(removeAt, lines.length - MAX_LINES);
								}
							},
							onInfo: () => {
								if (active) {
									// Setup may not have created the bridge/log source yet.
									conn = 'reconnecting';
									return;
								}
								notRunning = true;
								conn = 'idle';
								stopped = true;
							}
						},
						ctrl.signal
					);
					if (!stopped && !ctrl.signal.aborted) conn = 'reconnecting';
				} catch (err) {
					if (stopped || ctrl.signal.aborted) return;
					// A persistent client error (401/403/404) won't fix itself on retry, so
					// stop instead of looping every second and spamming the server.
					if (err instanceof ApiError && [401, 403, 404].includes(err.status)) {
						conn = 'idle';
						stopped = true;
						return;
					}
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
	class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] {compact ? 'p-4' : 'p-5'}"
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
		role="region"
		aria-label="Server logs"
		class="overflow-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] p-3 font-mono text-xs leading-relaxed {compact ? 'h-44' : 'h-72'}"
	>
		{#if lines.length > 0}
			{#each lines as line}
				<div class="whitespace-pre-wrap break-all text-[var(--color-ink-muted)]">{line}</div>
			{/each}
		{:else if notRunning}
			<p class="text-[var(--color-ink-dim)]">Server isn't running — no live logs.</p>
		{:else}
			<p class="text-[var(--color-ink-dim)]">Waiting for output…</p>
		{/if}
	</div>
</div>
