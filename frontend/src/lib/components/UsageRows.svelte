<script lang="ts">
	// One breakdown listing, in two view styles.
	//
	// Servers and tools normalize to the same `UsageRow` (see lib/usage.ts), so this
	// renders both: `table` for reading exact numbers and timestamps, `bars` for
	// seeing the shape of the distribution at a glance. A row with zero calls is
	// deliberately still a row — finding it is the point.
	import type { UsageRow } from '$lib/usage';
	import { peakCalls } from '$lib/usage';

	let {
		rows,
		style = 'table',
		/** Column heading for the label column ("Server" / "Tool"). */
		labelHeading = 'Name',
		emptyMessage = 'Nothing to show.'
	}: {
		rows: UsageRow[];
		style?: 'table' | 'bars';
		labelHeading?: string;
		emptyMessage?: string;
	} = $props();

	const peak = $derived(peakCalls(rows));

	function formatLastCall(iso: string | null): string {
		if (!iso) return 'never';
		const at = new Date(iso);
		if (Number.isNaN(at.getTime())) return iso;
		return at.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit'
		});
	}
</script>

{#if rows.length === 0}
	<p class="py-2 text-xs text-[var(--color-ink-dim)]">{emptyMessage}</p>
{:else if style === 'bars'}
	<ul class="flex flex-col gap-1.5" data-testid="usage-rows-bars">
		{#each rows as row (row.key)}
			<li class="flex flex-col gap-1" data-testid="usage-row" data-calls={row.calls}>
				<div class="flex items-baseline justify-between gap-3 text-xs">
					<span class="flex min-w-0 items-baseline gap-1.5">
						{#if row.href}
							<a
								href={row.href}
								class="truncate font-mono text-[var(--color-ink)] underline-offset-2 hover:underline"
							>
								{row.label}
							</a>
						{:else}
							<span class="truncate font-mono text-[var(--color-ink)]">{row.label}</span>
						{/if}
						{#if row.sublabel}
							<span class="shrink-0 text-[10px] text-[var(--color-ink-dim)]">{row.sublabel}</span>
						{/if}
						{#if row.badge}
							<span class="shrink-0 text-[10px] text-[var(--color-ink-dim)]">· {row.badge}</span>
						{/if}
					</span>
					<span
						class="shrink-0 font-mono"
						style={row.calls === 0
							? 'color: var(--color-ink-dim);'
							: 'color: var(--color-ink);'}
					>
						{row.calls}
					</span>
				</div>
				<!-- Proportional to the busiest row, so the listing reads as a distribution. -->
				<div class="h-1.5 w-full rounded-full bg-[var(--color-surface-2)]">
					<div
						class="h-full rounded-full"
						style={`width: ${(row.calls / peak) * 100}%; background-color: ${
							row.calls === 0 ? 'transparent' : 'var(--color-accent)'
						};`}
					></div>
				</div>
			</li>
		{/each}
	</ul>
{:else}
	<div class="overflow-x-auto">
		<table class="w-full text-xs" data-testid="usage-rows-table">
			<thead>
				<tr class="text-left text-[var(--color-ink-dim)]">
					<th class="pb-1 font-medium">{labelHeading}</th>
					<th class="pb-1 text-right font-medium">Calls</th>
					<th class="pb-1 text-right font-medium">Last call</th>
				</tr>
			</thead>
			<tbody>
				{#each rows as row (row.key)}
					<tr
						class="border-t border-[var(--color-line)]"
						data-testid="usage-row"
						data-calls={row.calls}
					>
						<td class="py-1.5 pr-3">
							<span class="flex flex-wrap items-baseline gap-1.5">
								{#if row.href}
									<a
										href={row.href}
										class="font-mono break-all text-[var(--color-ink)] underline-offset-2 hover:underline"
									>
										{row.label}
									</a>
								{:else}
									<span class="font-mono break-all text-[var(--color-ink)]">{row.label}</span>
								{/if}
								{#if row.sublabel}
									<span class="text-[10px] text-[var(--color-ink-dim)]">{row.sublabel}</span>
								{/if}
								{#if row.badge}
									<span class="text-[10px] text-[var(--color-ink-dim)]">· {row.badge}</span>
								{/if}
								{#if row.meta}
									<span class="text-[10px] text-[var(--color-ink-dim)]">· {row.meta}</span>
								{/if}
							</span>
						</td>
						<td
							class="py-1.5 text-right font-mono"
							style={row.calls === 0
								? 'color: var(--color-ink-dim);'
								: 'color: var(--color-ink);'}
						>
							{row.calls}
						</td>
						<td class="py-1.5 text-right whitespace-nowrap text-[var(--color-ink-dim)]">
							{formatLastCall(row.lastCall)}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}
