<script lang="ts">
	// Stub target for the dashboard "Add server" CTA. The real add-server flow
	// (runner config, transports, env) lands in a later milestone.
</script>

<section class="mx-auto flex max-w-xl flex-col items-center gap-6 py-16 text-center">
	<a
		href="/"
		class="inline-flex items-center gap-1.5 self-start text-sm text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
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
			<path d="M19 12H5M12 19l-7-7 7-7" />
		</svg>
		Back to servers
	</a>

	<div
		class="flex size-16 items-center justify-center rounded-2xl border border-[var(--color-line-strong)] bg-[var(--color-surface-2)]"
	>
		<svg
			class="size-8 text-[var(--color-accent)]"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M12 5v14M5 12h14" />
		</svg>
	</div>

	<div class="space-y-2">
		<h1 class="text-xl font-semibold tracking-tight text-[var(--color-ink)]">
			Add server — coming soon
		</h1>
		<p class="text-sm text-[var(--color-ink-muted)]">
			The flow to register a new MCP server is on its way. You'll point
			mcpelevator at an npx, uvx, command, or docker runner and it handles the
			rest.
		</p>
	</div>
</section>
