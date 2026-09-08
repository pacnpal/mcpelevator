<script lang="ts">
	// The one restart control in the UI. Every place that offers a restart — a server's
	// detail header, its dashboard card, a group row in Settings — renders THIS component,
	// so what "restart" means (which endpoint, the in-flight state, the wording, the error
	// toast) is defined once. Callers only vary the target and the skin.
	import { errorMessage, restartGroup, restartServer } from '$lib/api';
	import type { ServerSummary } from '$lib/types';
	import { flashToast } from '$lib/toast.svelte';

	let {
		target,
		disabled = false,
		variant = 'button',
		size = 'md',
		busy = $bindable(false),
		onrestarted,
		onerror
	}: {
		/** What to bounce: one server, or every enabled member of a group. */
		target: { kind: 'server'; id: string } | { kind: 'group'; name: string };
		/** Gate from the caller's own in-flight work (a start/stop, a delete, an apply). */
		disabled?: boolean;
		/** `button` is the standalone secondary button; `menuitem` a row in a kebab menu. */
		variant?: 'button' | 'menuitem';
		/** Match the surrounding controls: `md` alongside page-header actions, `sm` in
		 * denser rows (the groups list). Ignored by the `menuitem` variant. */
		size?: 'sm' | 'md';
		/** True while the restart is in flight. Bindable, so a caller can gate its own
		 * work on it — a restart bounces the bridge like a stop or a tool Apply does. */
		busy?: boolean;
		/** Called after a successful restart — with the refreshed summary for a server
		 * target, and with nothing for a group (which bounces many servers at once). */
		onrestarted?: (next?: ServerSummary) => void;
		/** Overrides the default toast so a caller with its own error surface can use it. */
		onerror?: (message: string) => void;
	} = $props();

	const label = $derived(busy ? 'Restarting' : 'Restart');
	const title = $derived(
		target.kind === 'group'
			? 'Restart every enabled member so the bundle picks up their current tools'
			: "Restart this server so it picks up the upstream's current tools"
	);

	async function run() {
		if (busy || disabled) return;
		busy = true;
		try {
			if (target.kind === 'server') {
				// Call, THEN notify — never `onrestarted?.(await restartServer(…))`: optional
				// invocation short-circuits its arguments, so with no callback attached the
				// restart would never be sent at all.
				const next = await restartServer(target.id);
				onrestarted?.(next);
			} else {
				const result = await restartGroup(target.name);
				onrestarted?.();
				// A group answers with ids, not summaries, so the toast is the only account
				// of what happened — including the members whose teardown failed, which the
				// backend reports rather than aborting the rest of the batch.
				const failed = result.failed?.length ?? 0;
				const bounced = result.restarted.length;
				const tail = failed > 0 ? ` ${failed} failed to stop — check its logs.` : '';
				flashToast(
					(bounced === 0
						? `No enabled members in ${result.name} were restarted.`
						: `Restarting ${bounced} member${bounced === 1 ? '' : 's'} of ${result.name}.`) +
						tail,
					failed > 0 ? 'error' : 'info'
				);
			}
		} catch (err) {
			const message = errorMessage(err);
			if (onerror) onerror(message);
			else flashToast(message);
		} finally {
			// Always clear: a caller may renavigate mid-flight (this component is reused
			// across same-route navigations), and a stuck-busy button would outlive the request.
			busy = false;
		}
	}
</script>

{#snippet icon(cls: string)}
	{#if busy}
		<svg class="{cls} animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
			<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
			<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
		</svg>
	{:else}
		<svg
			class={cls}
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M3 12a9 9 0 0 1 15.3-6.4L21 8" />
			<path d="M21 3v5h-5" />
			<path d="M21 12a9 9 0 0 1-15.3 6.4L3 16" />
			<path d="M3 21v-5h5" />
		</svg>
	{/if}
{/snippet}

{#if variant === 'menuitem'}
	<button
		type="button"
		role="menuitem"
		onclick={run}
		disabled={busy || disabled}
		aria-busy={busy}
		{title}
		class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-[var(--color-ink-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70"
	>
		{@render icon('size-4')}
		{label}
	</button>
{:else}
	<button
		type="button"
		onclick={run}
		disabled={busy || disabled}
		aria-busy={busy}
		{title}
		class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70 {size ===
		'sm'
			? 'px-3 py-1.5 text-xs'
			: 'px-3.5 py-2 text-sm'}"
	>
		{@render icon(size === 'sm' ? 'size-3.5' : 'size-4')}
		{label}
	</button>
{/if}
