<script lang="ts">
	// The usage series, drawn with LayerChart — the Svelte 5 charting library
	// (runes + snippets) that shadcn-svelte's own chart components are built on.
	// It brings the parts that are tedious and easy to get subtly wrong by hand:
	// d3 scales, real axes with tick formatting, a hover crosshair with a tooltip,
	// and responsive resize — while still rendering plain SVG we style with the
	// app's tokens (see the `.lc-*` mapping in app.css, which points LayerChart's
	// surface/primary variables at mcpelevator's zinc + emerald palette).
	//
	// Two quantities, always: tool calls and the non-tool traffic underneath them,
	// because "connected but called nothing" is the comparison the panel exists to
	// make. `bars` stacks them per bucket; `area` reads a long window as a shape.
	import { AreaChart, BarChart } from 'layerchart';

	import type { UsagePoint } from '$lib/types';
	import { tickLabel, toChartPoints } from '$lib/usage';

	let {
		series,
		bucketSeconds,
		mode = 'bars',
		height = 'h-24',
		axis = true
	}: {
		series: UsagePoint[];
		/** 3600 = hourly buckets, 86400 = daily. Decides how ticks are labelled. */
		bucketSeconds: number;
		mode?: 'bars' | 'line';
		/** Tailwind height class for the plot area. */
		height?: string;
		/** Axes and grid off for a sparkline-sized chart. */
		axis?: boolean;
	} = $props();

	const hourly = $derived(bucketSeconds < 86400);

	// LayerChart wants real Dates for a time axis; the API hands over ISO strings.
	const data = $derived(toChartPoints(series));

	const chartSeries = $derived([
		{
			key: 'calls',
			label: 'Tool calls',
			value: (d: { calls: number }) => d.calls,
			color: 'var(--color-accent)'
		},
		{
			key: 'other',
			label: 'Other requests',
			value: (d: { other: number }) => d.other,
			color: 'color-mix(in oklab, var(--color-ink-dim) 45%, transparent)'
		}
	]);

	const tick = $derived((value: Date | string | number) => tickLabel(value, hourly));

	// The tooltip header names the bucket the reader is hovering. Its default is a
	// bare date, which says nothing useful about WHICH hour on an hourly window.
	const bucketLabel = $derived((value: Date | string | number) => {
		const at = value instanceof Date ? value : new Date(value);
		if (Number.isNaN(at.getTime())) return String(value);
		return hourly
			? at.toLocaleString(undefined, {
					month: 'short',
					day: 'numeric',
					hour: 'numeric',
					minute: '2-digit'
				})
			: at.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
	});

	// Whole numbers only: half a call doesn't exist, and the default tick
	// generator will happily offer 0.5 on a quiet window.
	const wholeNumbers = { format: (v: number) => (Number.isInteger(v) ? String(v) : '') };
</script>

<div class={height}>
	{#if mode === 'line'}
		<AreaChart
			{data}
			x="bucket"
			series={chartSeries}
			seriesLayout="stack"
			{axis}
			grid={axis}
			legend={false}
			props={{
				xAxis: { format: tick, ticks: 6 },
				yAxis: wholeNumbers,
				tooltip: { header: { format: bucketLabel } },
				area: { line: { class: 'stroke-2' }, 'fill-opacity': 0.15 }
			}}
		/>
	{:else}
		<BarChart
			{data}
			x="bucket"
			series={chartSeries}
			seriesLayout="stack"
			{axis}
			grid={axis}
			legend={false}
			bandPadding={0.25}
			props={{
				xAxis: { format: tick, ticks: 6 },
				yAxis: wholeNumbers,
				tooltip: { header: { format: bucketLabel } },
				bars: { radius: 2, rounded: 'top', strokeWidth: 0 }
			}}
		/>
	{/if}
</div>
