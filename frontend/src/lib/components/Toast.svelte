<script lang="ts">
	let {
		message,
		tone = 'error',
		onclose
	}: {
		message: string;
		tone?: 'error' | 'info';
		onclose?: () => void;
	} = $props();

	const accent = $derived(
		tone === 'error' ? 'var(--color-state-failed)' : 'var(--color-accent)'
	);
</script>

<div
	role="alert"
	class="animate-rise-in pointer-events-auto flex items-start gap-3 rounded-xl border bg-[var(--color-elevated)]/95 px-4 py-3 shadow-2xl backdrop-blur"
	style="border-color: color-mix(in oklab, {accent} 35%, var(--color-line));"
>
	<span
		class="mt-0.5 size-2 shrink-0 rounded-full"
		style="background-color: {accent};"
	></span>
	<p class="flex-1 text-sm leading-snug text-[var(--color-ink)]">{message}</p>
	{#if onclose}
		<button
			type="button"
			onclick={onclose}
			aria-label="Dismiss"
			class="-mr-1 -mt-0.5 rounded-md p-1 text-[var(--color-ink-dim)] transition hover:text-[var(--color-ink)]"
		>
			<svg
				class="size-4"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				aria-hidden="true"
			>
				<path d="M18 6 6 18M6 6l12 12" />
			</svg>
		</button>
	{/if}
</div>
