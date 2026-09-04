<script lang="ts">
	// Small multiples: the window split by server, one miniature chart each.
	//
	// This is the "who is this traffic?" view. It is NOT a stacked multi-hue chart,
	// deliberately: mcpelevator's design system locks a single emerald accent and
	// supplies no categorical palette, so a stack here would have to invent five
	// hues the product doesn't own. Faceting is the documented alternative when you
	// have no colour slots — and it compares servers better anyway, since each
	// server's shape is read on its own baseline instead of riding on the one below.
	//
	// Every panel shares ONE scale (the busiest bucket across all servers), so the
	// panels are comparable at a glance; a per-panel scale would make a server with
	// three calls look as busy as one with three hundred.
	import type { UsageBand, UsagePoint } from '$lib/types';

	let {
		series,
		bands,
		bucketSeconds
	}: {
		series: UsagePoint[];
		/** Each band's `points` is aligned index-for-index with `series`. */
		bands: UsageBand[];
		bucketSeconds: number;
	} = $props();

	const hourly = $derived(bucketSeconds < 86400);
	const peak = $derived(
		Math.max(1, ...bands.flatMap((band) => band.points))
	);

	function total(band: UsageBand): number {
		return band.points.reduce((sum, value) => sum + value, 0);
	}

	function label(iso: string): string {
		const at = new Date(iso);
		if (Number.isNaN(at.getTime())) return iso;
		return hourly
			? at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
			: at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function tooltip(index: number, calls: number): string {
		const when = series[index] ? label(series[index].bucket) : '';
		return `${when} — ${calls} call${calls === 1 ? '' : 's'}`;
	}
</script>

<div class="grid gap-3 sm:grid-cols-2" data-testid="usage-sparklines">
	{#each bands as band (band.slug)}
		<figure
			class="m-0 flex flex-col gap-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-3"
			data-testid="usage-facet"
			data-slug={band.slug}
		>
			<figcaption class="flex items-baseline justify-between gap-2">
				<span class="truncate text-xs text-[var(--color-ink)]">{band.name}</span>
				<span class="shrink-0 font-mono text-xs text-[var(--color-ink-muted)]">
					{total(band)}
				</span>
			</figcaption>
			<div
				class="flex h-10 items-end gap-px"
				role="img"
				aria-label={`${band.name}: ${total(band)} tool calls`}
			>
				{#each band.points as calls, index (index)}
					<!-- Capped like every other column in the app: a week of daily buckets
					     across a wide facet must read as marks, not slabs. -->
					<div
						class="flex h-full min-w-0 flex-1 justify-center border-b border-[var(--color-line)]"
						title={tooltip(index, calls)}
						data-testid="facet-bar"
						data-calls={calls}
					>
						<div class="flex h-full w-full max-w-4 flex-col justify-end">
							{#if calls > 0}
								<div
									class="min-h-[2px] rounded-t-[2px]"
									style={`height: ${(calls / peak) * 100}%; background-color: var(--color-accent);`}
								></div>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		</figure>
	{/each}
</div>
