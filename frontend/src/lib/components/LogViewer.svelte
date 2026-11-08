<script lang="ts">
	import { untrack } from 'svelte';

	import { ApiError, streamLogs } from '#lib/api.js';
	import type { ServerState, StartupStatus } from '#lib/types.js';

	let {
		serverId,
		serverState,
		startupStatus = null,
		compact = false,
		slug = null
	}: {
		serverId: string;
		serverState: ServerState;
		startupStatus?: StartupStatus | null;
		compact?: boolean;
		slug?: string | null;
	} = $props();

	const MAX_LINES = 1000;
	const TRUNCATION_MARKER = 'omitted (buffer limit)';
	// Strip terminal color codes (fastmcp/uvicorn log in ANSI) for a clean pane.
	const ANSI = /\x1b\[[0-9;]*m/g;

	type Conn = 'idle' | 'connecting' | 'live' | 'reconnecting';

	let lines = $state<string[]>([]);
	let conn = $state<Conn>('idle');
	let notRunning = $state(false);
	let paused = $state(false);
	let stuckToBottom = $state(true);
	let pane = $state<HTMLDivElement | undefined>(undefined);
	let seenServerId = '';
	let seenActivation: string | null = null;

	// Memoized: the server page passes props straight off the server object it re-fetches
	// every poll (each second during startup), so reading a prop directly in the stream
	// effect would re-run it — abort the stream and reopen it — on every poll. A derived
	// only notifies when its value changes.
	const streamServerId = $derived(serverId);
	const startupActive = $derived(startupStatus !== null);
	const activationStartedAt = $derived(startupStatus?.activation_started_at ?? null);
	const resetKey = $derived(`${streamServerId}\u0000${activationStartedAt ?? ''}`);
	// Setup can produce logs before the bridge changes observed runtime state.
	const serverUp = $derived(startupActive || serverState !== 'stopped');
	// Pause stops the live tail entirely (drops the stream) rather than buffering,
	// so the pane freezes for reading/selection; Resume reopens and the endpoint
	// replays its backlog, swapped in atomically (no empty blink, see run()).
	const shouldConnect = $derived(serverUp && !paused);

	const dot = $derived.by(() => {
		if (paused && serverUp)
			return { color: 'var(--color-state-starting)', pulse: false, label: 'paused' };
		if (notRunning || conn === 'idle')
			return { color: 'var(--color-state-stopped)', pulse: false, label: 'idle' };
		if (conn === 'live') return { color: 'var(--color-state-running)', pulse: false, label: 'live' };
		return { color: 'var(--color-state-starting)', pulse: true, label: 'connecting' };
	});

	function onScroll() {
		if (!pane) return;
		stuckToBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 24;
	}

	let copied = $state(false);
	let copyTimer: ReturnType<typeof setTimeout> | undefined;

	async function copyLogs() {
		const text = lines.join('\n');
		if (!text) return;
		try {
			await navigator.clipboard.writeText(text);
		} catch {
			// Fallback for non-secure contexts (LAN/http) where Clipboard API is blocked.
			const ta = document.createElement('textarea');
			ta.value = text;
			ta.style.position = 'fixed';
			ta.style.opacity = '0';
			document.body.appendChild(ta);
			ta.select();
			try {
				document.execCommand('copy');
			} catch {
				/* give up silently */
			}
			ta.remove();
		}
		copied = true;
		clearTimeout(copyTimer);
		copyTimer = setTimeout(() => (copied = false), 1400);
	}

	$effect(() => () => clearTimeout(copyTimer));

	function downloadLogs() {
		const text = lines.join('\n');
		if (!text) return;
		const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
		const url = URL.createObjectURL(new Blob([text + '\n'], { type: 'text/plain' }));
		const a = document.createElement('a');
		a.href = url;
		a.download = `${slug ?? serverId}-logs-${stamp}.txt`;
		a.click();
		URL.revokeObjectURL(url);
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

	// Stream lifecycle reopens when the server, activation, or connection gate changes —
	// and ONLY then: those three memoized values are the effect's whole dependency set,
	// and the stream loop itself starts untracked, so nothing it reads on the way in can
	// make a page poll tear the stream down.
	// Fetch + ReadableStream (not EventSource) so the Authorization header is sent.
	// Reconnect is manual: a dropped stream retries after a short backoff until the
	// effect is torn down (server / activation change, gate change, unmount).
	$effect(() => {
		void resetKey;
		const id = streamServerId;
		const active = startupActive;
		if (!shouldConnect) {
			untrack(() => (conn = 'idle'));
			return;
		}
		const ctrl = new AbortController();
		let stopped = false;
		untrack(() => {
			notRunning = false;
			conn = 'connecting';
			void run();
		});

		async function run() {
			// The endpoint replays its bounded activation backlog on every open. Swap
			// it in atomically on the first replayed line instead of clearing up front,
			// so a reopen (the supervisor replaces the unit on every retry/idle cycle)
			// doesn't blink the pane empty between the clear and the refill.
			let replaceOnNext = false;
			while (!stopped && !ctrl.signal.aborted) {
				try {
					await streamLogs(
						id,
						{
							onOpen: () => {
								replaceOnNext = true;
								notRunning = false;
								conn = 'live';
							},
							onLine: (line) => {
								const clean = line.replace(ANSI, '');
								if (replaceOnNext) {
									lines = [clean];
									replaceOnNext = false;
								} else {
									lines.push(clean);
								}
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
								// Nothing was replayed this open (no backlog), so drop any
								// lines left from a prior activation. A replayed terminal
								// backlog (replaceOnNext already consumed) stays visible.
								if (replaceOnNext) lines = [];
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
		}

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
		<div class="flex items-center gap-2">
			<button
				type="button"
				onclick={() => (paused = !paused)}
				disabled={!serverUp}
				class="rounded-md border border-[var(--color-line)] px-2.5 py-1 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-40"
			>
				{paused ? 'Resume' : 'Pause'}
			</button>
			<button
				type="button"
				onclick={copyLogs}
				disabled={lines.length === 0}
				class="rounded-md border border-[var(--color-line)] px-2.5 py-1 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-40"
			>
				{copied ? 'Copied' : 'Copy'}
			</button>
			<button
				type="button"
				onclick={downloadLogs}
				disabled={lines.length === 0}
				class="rounded-md border border-[var(--color-line)] px-2.5 py-1 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-40"
			>
				Download
			</button>
			<button
				type="button"
				onclick={() => (lines = [])}
				disabled={lines.length === 0}
				class="rounded-md border border-[var(--color-line)] px-2.5 py-1 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-40"
			>
				Clear
			</button>
		</div>
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
