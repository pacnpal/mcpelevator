<script lang="ts">
	// A dependency-free bar chart for one server's usage series.
	//
	// The backend hands over a DENSE series (quiet buckets included) already rolled
	// up to the right width, so this only maps counts to heights — no windowing or
	// gap-filling in the browser. Each bar stacks tool calls (accent) over non-tool
	// traffic (muted), which is what makes "connected but never called a tool"
	// visible at a glance.
	import type { UsagePoint } from '$lib/types';

	let {
		series,
		bucketSeconds
	}: {
		series: UsagePoint[];
		/** 3600 = hourly buckets, 86400 = daily. Decides how ticks are labelled. */
		bucketSeconds: number;
	} = $props();

	const hourly = $derived(bucketSeconds < 86400);
	// Never divide by zero, and keep an all-quiet window flat rather than making a
	// single stray request look like a full-height spike.
	const peak = $derived(Math.max(1, ...series.map((p) => p.calls + p.other)));
	const totalCalls = $derived(series.reduce((sum, p) => sum + p.calls, 0));

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

	function height(value: number): string {
		return `${(value / peak) * 100}%`;
	}
</script>

<figure class="m-0 flex flex-col gap-1.5">
	<div
		class="flex h-24 items-end gap-px"
		role="img"
		aria-label={`${totalCalls} tool calls across ${series.length} ${
			hourly ? 'hourly' : 'daily'
		} buckets`}
	>
		{#each series as point (point.bucket)}
			<!-- The bottom border doubles as the axis, so a quiet bucket still shows a
			     tick instead of a gap. -->
			<div
				class="flex h-full min-w-0 flex-1 flex-col justify-end border-b border-[var(--color-line)]"
				title={tooltip(point)}
				data-testid="usage-bar"
				data-calls={point.calls}
			>
				{#if point.calls > 0}
					<div
						class="min-h-[3px] rounded-t-[2px]"
						style={`height: ${height(point.calls)}; background-color: var(--color-accent);`}
					></div>
				{/if}
				{#if point.other > 0}
					<div
						class="min-h-[2px]"
						style={`height: ${height(point.other)}; background-color: color-mix(in oklab, var(--color-ink-dim) 35%, transparent);`}
					></div>
				{/if}
			</div>
		{/each}
	</div>
	<figcaption class="flex justify-between text-[10px] text-[var(--color-ink-dim)]">
		<span>{series.length ? label(series[0].bucket) : ''}</span>
		<span>{series.length ? label(series[series.length - 1].bucket) : ''}</span>
	</figcaption>
</figure>
