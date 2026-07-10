<script lang="ts">
	// Wordmark for mcpelevator. The mark is a pair of elevator doors parting, where
	// the negative space between the panels forms an up arrow — stdio going up to
	// HTTP. One-color variant (no indicator lamp) per the identity's small-size
	// rules; a single evenodd path, themeable via the accent token.
	let { compact = false }: { compact?: boolean } = $props();
</script>

<span class="inline-flex items-center gap-2.5 select-none">
	<span
		class="flex size-7 items-center justify-center rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-surface-2)]"
		aria-hidden="true"
	>
		<svg class="size-4" viewBox="22 30 84 76" fill="none" aria-hidden="true">
			<path
				fill="var(--color-accent)"
				fill-rule="evenodd"
				d="M34 32 H94 A10 10 0 0 1 104 42 V94 A10 10 0 0 1 94 104 H34 A10 10 0 0 1 24 94 V42 A10 10 0 0 1 34 32 Z M60 32 L68 32 L88 58 L72 58 L72 104 L56 104 L56 58 L40 58 Z"
			/>
		</svg>
	</span>
	{#if !compact}
		<span class="text-[15px] font-semibold tracking-tight text-[var(--color-ink)]">
			<span style="color: var(--color-accent);">mcp</span>elevator
		</span>
	{/if}
</span>
