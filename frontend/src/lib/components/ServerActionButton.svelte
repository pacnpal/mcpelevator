<script lang="ts">
	// The one start/stop/retry control in the UI. Which of the three a server is offered
	// is decided in exactly one place (`primaryServerAction`), and what pressing it does
	// — the endpoint, the in-flight state, the wording — lives here, so the dashboard
	// card, the server page header, and a group's member rows can't drift apart.
	import { disableServer, enableServer, errorMessage, retryServer } from '$lib/api';
	import { primaryServerAction } from '$lib/startup';
	import type { ServerSummary } from '$lib/types';

	let {
		server,
		disabled = false,
		size = 'md',
		busy = $bindable(false),
		onchange,
		onerror
	}: {
		server: ServerSummary;
		/** Gate from the caller's own in-flight work (a delete, a clone, a tool Apply). */
		disabled?: boolean;
		/** `md` alongside page-header actions, `sm` in denser rows (cards, member lists). */
		size?: 'sm' | 'md';
		/** True while the action is in flight. Bindable, so a caller can gate its own
		 * work (a tool Apply, a clone, a status poll) on the lifecycle op it started. */
		busy?: boolean;
		/** The refreshed summary after a successful action. */
		onchange?: (next: ServerSummary) => void;
		/** Overrides nothing — the caller owns how a failure is surfaced. */
		onerror?: (message: string) => void;
	} = $props();

	const action = $derived(primaryServerAction(server));
	const label = $derived(
		busy
			? action === 'stop'
				? 'Stopping'
				: action === 'retry'
					? 'Retrying'
					: 'Starting'
			: action === 'stop'
				? 'Stop'
				: action === 'retry'
					? 'Retry'
					: 'Start'
	);

	async function run() {
		if (busy || disabled) return;
		busy = true;
		// Capture the target: this component is reused across same-route navigations
		// (clone, sidebar), so the caller matches the response against the id it asked for.
		const id = server.id;
		try {
			const next =
				action === 'stop'
					? await disableServer(id)
					: action === 'retry'
						? await retryServer(id)
						: await enableServer(id);
			onchange?.(next);
		} catch (err) {
			onerror?.(errorMessage(err));
		} finally {
			busy = false;
		}
	}
</script>

<button
	type="button"
	onclick={run}
	disabled={busy || disabled}
	aria-busy={busy}
	class="inline-flex shrink-0 items-center gap-1.5 rounded-lg font-semibold transition active:translate-y-px disabled:cursor-wait disabled:opacity-70 {size ===
	'sm'
		? 'px-3 py-1.5 text-xs'
		: 'px-3.5 py-2 text-sm'}"
	style={action === 'stop'
		? 'color: var(--color-ink-muted); border: 1px solid var(--color-line);'
		: 'color: var(--color-accent-ink); background-color: var(--color-accent);'}
>
	{#if busy}
		<svg
			class="{size === 'sm' ? 'size-3.5' : 'size-4'} animate-spin"
			viewBox="0 0 24 24"
			fill="none"
			aria-hidden="true"
		>
			<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
			<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
		</svg>
	{:else if action === 'stop'}
		<svg
			class={size === 'sm' ? 'size-3.5' : 'size-4'}
			viewBox="0 0 24 24"
			fill="currentColor"
			aria-hidden="true"
		>
			<rect x="7" y="7" width="10" height="10" rx="1.5" />
		</svg>
	{:else if action === 'retry'}
		<svg
			class={size === 'sm' ? 'size-3.5' : 'size-4'}
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />
		</svg>
	{:else}
		<svg
			class={size === 'sm' ? 'size-3.5' : 'size-4'}
			viewBox="0 0 24 24"
			fill="currentColor"
			aria-hidden="true"
		>
			<path d="M8 5v14l11-7z" />
		</svg>
	{/if}
	{label}
</button>
