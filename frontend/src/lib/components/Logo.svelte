<script lang="ts">
	// Wordmark for mcpelevator. The mark is a stack of three rising bars — an
	// "elevator going up" glyph — paired with the wordmark. Simple geometric
	// shapes only, themeable via currentColor / accent token.
	let { compact = false }: { compact?: boolean } = $props();
</script>

<span class="inline-flex items-center gap-2.5 select-none">
	<span
		class="flex size-7 items-center justify-center rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-surface-2)]"
		aria-hidden="true"
	>
		<svg class="size-4" viewBox="0 0 24 24" fill="none" aria-hidden="true">
			<path
				d="M12 4 7 10h10z"
				fill="var(--color-accent)"
			/>
			<rect x="6" y="13" width="12" height="2" rx="1" fill="var(--color-ink-muted)" />
			<rect x="8" y="17" width="8" height="2" rx="1" fill="var(--color-ink-dim)" />
		</svg>
	</span>
	{#if !compact}
		<span class="text-[15px] font-semibold tracking-tight text-[var(--color-ink)]">
			<span style="color: var(--color-accent);">mcp</span>elevator
		</span>
	{/if}
</span>
