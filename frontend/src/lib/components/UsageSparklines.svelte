<script lang="ts">
	// Small multiples: the window split by server, one miniature LayerChart each.
	//
	// This is the "who is this traffic?" view. It is NOT a stacked multi-hue chart,
	// deliberately: mcpelevator's design system locks a single emerald accent and
	// supplies no categorical palette, so a stack here would have to invent five
	// hues the product doesn't own. Faceting is the documented alternative when you
	// have no colour slots — and it compares servers better anyway, since each
	// server's shape is read on its own baseline instead of riding on the one below.
	//
	// Every panel shares ONE y domain (the busiest bucket across all servers), so
	// the panels are comparable at a glance; a per-panel scale would make a server
	// with three calls look as busy as one with three hundred.
	import { BarChart } from 'layerchart';

	import type { UsageBand, UsagePoint } from '$lib/types';
	import { facetPeak, tickLabel, toFacetPoints } from '$lib/usage';

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
	// Never a zero domain, so an all-quiet window renders flat instead of dividing
	// by nothing.
	const peak = $derived(facetPeak(bands));

	function total(band: UsageBand): number {
		return band.points.reduce((sum, value) => sum + value, 0);
	}

	const tick = $derived((value: Date | string | number) => tickLabel(value, hourly));

	// Identity for the single folded band, which has no server of its own.
	const OTHER_KEY = '__other__';
</script>

<div class="grid gap-3 sm:grid-cols-2" data-testid="usage-sparklines">
	<!-- Keyed by server id, not slug: the folded remainder carries the label "other",
	     and a real server may legitimately be slugged `other` too. Two bands with the
	     same key is a hard Svelte error, which would take the whole view down. -->
	{#each bands as band (band.server_id ?? OTHER_KEY)}
		<figure
			class="m-0 flex flex-col gap-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-3"
			data-testid="usage-facet"
			data-slug={band.server_id ?? OTHER_KEY}
			data-total={total(band)}
		>
			<figcaption class="flex items-baseline justify-between gap-2">
				<span class="truncate text-xs text-[var(--color-ink)]">{band.name}</span>
				<span class="shrink-0 font-mono text-xs text-[var(--color-ink-muted)]">
					{total(band)}
				</span>
			</figcaption>
			<div class="h-14">
				<BarChart
					data={toFacetPoints(band, series)}
					x="bucket"
					y="calls"
					yDomain={[0, peak]}
					axis={false}
					grid={false}
					rule={false}
					bandPadding={0.25}
					props={{
						bars: {
							radius: 2,
							rounded: 'top',
							strokeWidth: 0,
							fill: 'var(--color-accent)'
						},
						tooltip: { context: { mode: 'band' } },
						xAxis: { format: tick }
					}}
				/>
			</div>
		</figure>
	{/each}
</div>
