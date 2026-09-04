<script lang="ts">
	// Activity by weekday and hour — the punchcard.
	//
	// "How much" is already answered by the series; this answers "when", which is
	// what tells you whether an agent runs on a schedule, during your working day,
	// or in one nightly burst. Magnitude, so the colour job is SEQUENTIAL: one hue
	// (the app's accent), light to dark, in four steps with a scale legend — never
	// a rainbow, never a categorical hue.
	//
	// Hours are the READER's local hours: the API reports UTC precisely because
	// only the browser knows the timezone to bucket them into.
	import type { UsageHour } from '$lib/types';
	import { HEATMAP_DAYS, heatLevel, heatmap } from '$lib/usage';

	let { hourly }: { hourly: UsageHour[] } = $props();

	const grid = $derived(heatmap(hourly));

	// A fixed reference week (2026-06-01 was a Monday) rendered through the
	// viewer's locale, so day names are localized without a hardcoded list.
	const dayLabels = HEATMAP_DAYS.map((_, index) =>
		new Date(Date.UTC(2026, 5, 1 + index)).toLocaleDateString(undefined, {
			weekday: 'short',
			timeZone: 'UTC'
		})
	);
	const hours = Array.from({ length: 24 }, (_, hour) => hour);

	/** One hue, four steps. Step 0 is the empty surface, so a quiet hour reads as
	 * absence rather than as the lightest data value. */
	function fill(level: number): string {
		if (level === 0) return 'var(--color-surface-2)';
		const mix = [0, 22, 45, 70, 100][level];
		return `color-mix(in oklab, var(--color-accent) ${mix}%, var(--color-surface-2))`;
	}

	function cellTitle(row: number, hour: number, value: number): string {
		const end = (hour + 1) % 24;
		return `${dayLabels[row]} ${String(hour).padStart(2, '0')}:00–${String(end).padStart(
			2,
			'0'
		)}:00 — ${value} call${value === 1 ? '' : 's'}`;
	}
</script>

<figure class="m-0 flex flex-col gap-2">
	<div
		class="flex flex-col gap-[2px]"
		role="img"
		aria-label={`${grid.total} tool calls by weekday and hour, local time; busiest hour ${grid.peak}`}
	>
		{#each grid.cells as row, rowIndex (rowIndex)}
			<div class="flex items-center gap-2">
				<span
					class="w-8 shrink-0 text-right text-[10px] text-[var(--color-ink-dim)]"
					aria-hidden="true"
				>
					{dayLabels[rowIndex]}
				</span>
				<div class="flex min-w-0 flex-1 gap-[2px]">
					{#each row as value, hour (hour)}
						<div
							class="h-3.5 min-w-0 flex-1 rounded-[2px]"
							style={`background-color: ${fill(heatLevel(value, grid.peak))};`}
							title={cellTitle(rowIndex, hour, value)}
							data-testid="heatmap-cell"
							data-level={heatLevel(value, grid.peak)}
						></div>
					{/each}
				</div>
			</div>
		{/each}
		<!-- Hour axis: a tick every six hours keeps the labels from colliding. -->
		<div class="flex items-center gap-2">
			<span class="w-8 shrink-0" aria-hidden="true"></span>
			<div class="flex min-w-0 flex-1 gap-[2px]">
				{#each hours as hour (hour)}
					<span class="min-w-0 flex-1 text-[9px] text-[var(--color-ink-dim)]">
						{hour % 6 === 0 ? String(hour).padStart(2, '0') : ''}
					</span>
				{/each}
			</div>
		</div>
	</div>

	<figcaption
		class="flex flex-wrap items-center justify-between gap-2 text-[10px] text-[var(--color-ink-dim)]"
	>
		<span>Local time · busiest hour {grid.peak} call{grid.peak === 1 ? '' : 's'}</span>
		<span class="flex items-center gap-1.5">
			less
			{#each [0, 1, 2, 3, 4] as level (level)}
				<span
					class="size-2.5 rounded-[2px]"
					style={`background-color: ${fill(level)};`}
					aria-hidden="true"
				></span>
			{/each}
			more
		</span>
	</figcaption>
</figure>
