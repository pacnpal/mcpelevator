<script lang="ts">
	// A dependency-free chart for a usage series, in two view styles.
	//
	// The backend hands over a DENSE series (quiet buckets included) already rolled
	// up to the right width, so this only maps counts to geometry — no windowing or
	// gap-filling in the browser. Both styles show the same two quantities: tool
	// calls (accent) and non-tool traffic (muted), because "connected but called
	// nothing" is the comparison the whole panel exists to make.
	//
	// `bars` reads a short window bucket-by-bucket; `line` reads a long one as a
	// shape. Neither needs a charting library: bars are flex children, the line is
	// one inline SVG polyline pair.
	import type { UsagePoint } from '$lib/types';

	let {
		series,
		bucketSeconds,
		mode = 'bars',
		height = 'h-24'
	}: {
		series: UsagePoint[];
		/** 3600 = hourly buckets, 86400 = daily. Decides how ticks are labelled. */
		bucketSeconds: number;
		mode?: 'bars' | 'line';
		/** Tailwind height class for the plot area. */
		height?: string;
	} = $props();

	const hourly = $derived(bucketSeconds < 86400);
	// Never divide by zero, and keep an all-quiet window flat rather than making a
	// single stray request look like a full-height spike.
	const peak = $derived(Math.max(1, ...series.map((p) => p.calls + p.other)));
	const totalCalls = $derived(series.reduce((sum, p) => sum + p.calls, 0));

	// Line mode: an SVG viewBox in series coordinates, stretched by CSS. Points are
	// placed at bucket centres so the first and last aren't clipped at the edges.
	const VIEW_W = 100;
	const VIEW_H = 40;
	const points = $derived(
		series.map((point, i) => ({
			x: series.length > 1 ? (i / (series.length - 1)) * VIEW_W : VIEW_W / 2,
			calls: VIEW_H - (point.calls / peak) * VIEW_H,
			total: VIEW_H - ((point.calls + point.other) / peak) * VIEW_H
		}))
	);
	const callsPath = $derived(points.map((p) => `${p.x},${p.calls}`).join(' '));
	const totalPath = $derived(points.map((p) => `${p.x},${p.total}`).join(' '));
	const callsArea = $derived(
		points.length ? `${points[0].x},${VIEW_H} ${callsPath} ${points[points.length - 1].x},${VIEW_H}` : ''
	);

	function label(iso: string): string {
		const at = new Date(iso);
		if (Number.isNaN(at.getTime())) return iso;
		return hourly
			? at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
			: at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}

	function tooltip(point: UsagePoint): string {
		const calls = `${point.calls} tool call${point.calls === 1 ? '' : 's'}`;
		return `${label(point.bucket)} — ${calls}, ${point.other} other`;
	}

	function barHeight(value: number): string {
		return `${(value / peak) * 100}%`;
	}

	const ariaLabel = $derived(
		`${totalCalls} tool calls across ${series.length} ${hourly ? 'hourly' : 'daily'} buckets`
	);
</script>

<figure class="m-0 flex flex-col gap-1.5">
	{#if mode === 'line'}
		<div class={height} role="img" aria-label={ariaLabel} data-testid="usage-line">
			<svg
				viewBox="0 0 {VIEW_W} {VIEW_H}"
				preserveAspectRatio="none"
				class="h-full w-full overflow-visible"
				aria-hidden="true"
			>
				<polyline
					points={callsArea}
					fill="color-mix(in oklab, var(--color-accent) 18%, transparent)"
					stroke="none"
				/>
				<polyline
					points={totalPath}
					fill="none"
					stroke="color-mix(in oklab, var(--color-ink-dim) 45%, transparent)"
					stroke-width="0.6"
					vector-effect="non-scaling-stroke"
					stroke-linejoin="round"
				/>
				<polyline
					points={callsPath}
					fill="none"
					stroke="var(--color-accent)"
					stroke-width="1.2"
					vector-effect="non-scaling-stroke"
					stroke-linejoin="round"
				/>
			</svg>
		</div>
	{:else}
		<div class="{height} flex items-end gap-px" role="img" aria-label={ariaLabel}>
			{#each series as point (point.bucket)}
				<!-- The bottom border doubles as the axis, so a quiet bucket still shows a
				     tick instead of a gap. The column is capped rather than filling its
				     slot: a short window should leave air between marks, not draw slabs. -->
				<div
					class="flex h-full min-w-0 flex-1 justify-center border-b border-[var(--color-line)]"
					title={tooltip(point)}
					data-testid="usage-bar"
					data-calls={point.calls}
				>
					<div class="flex h-full w-full max-w-6 flex-col justify-end gap-[2px]">
						{#if point.calls > 0}
							<div
								class="min-h-[3px] rounded-t-[4px]"
								style={`height: ${barHeight(point.calls)}; background-color: var(--color-accent);`}
							></div>
						{/if}
						{#if point.other > 0}
							<div
								class="min-h-[2px]"
								style={`height: ${barHeight(point.other)}; background-color: color-mix(in oklab, var(--color-ink-dim) 35%, transparent);`}
							></div>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}
	<figcaption class="flex justify-between text-[10px] text-[var(--color-ink-dim)]">
		<span>{series.length ? label(series[0].bucket) : ''}</span>
		<span>{series.length ? label(series[series.length - 1].bucket) : ''}</span>
	</figcaption>
</figure>
