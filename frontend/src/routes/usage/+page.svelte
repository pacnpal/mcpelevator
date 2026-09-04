<script lang="ts">
	// The usage dashboard: what every server and every tool is actually being used
	// for, across the whole instance.
	//
	// One fetch per window (`GET /api/usage`) returns the totals, the series and both
	// breakdowns whole; the toolbar then filters and sorts in the browser, so typing
	// in the search box costs nothing and clearing it costs no refetch. The view
	// rules themselves live in `$lib/usage` — pure, and unit-tested there.
	import { errorMessage, getInstanceUsage } from '$lib/api';
	import type { InstanceUsage } from '$lib/types';
	import {
		DEFAULT_VIEW,
		USAGE_SORTS,
		applyView,
		countLabel,
		serverRows,
		toolRows,
		type UsageSort,
		type UsageView
	} from '$lib/usage';
	import UsageChart from '$lib/components/UsageChart.svelte';
	import UsageHeatmap from '$lib/components/UsageHeatmap.svelte';
	import UsageRows from '$lib/components/UsageRows.svelte';
	import UsageSparklines from '$lib/components/UsageSparklines.svelte';

	const RANGES = [
		{ days: 1, label: '24h' },
		{ days: 7, label: '7d' },
		{ days: 30, label: '30d' },
		{ days: 90, label: '90d' }
	];

	let usage = $state<InstanceUsage | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let days = $state(7);
	let chartMode = $state<'bars' | 'line' | 'servers'>('bars');
	const CHART_MODES = [
		{ value: 'bars', label: 'Bars' },
		{ value: 'line', label: 'Line' },
		{ value: 'servers', label: 'By server' }
	];
	let tab = $state<'servers' | 'tools'>('tools');
	let rowStyle = $state<'table' | 'bars'>('table');
	let view = $state<UsageView>({ ...DEFAULT_VIEW });

	// One request generation counter: a slow window switch must never overwrite the
	// result of a faster one the operator clicked afterwards.
	let generation = 0;

	async function load(window: number) {
		const mine = ++generation;
		loading = true;
		try {
			const result = await getInstanceUsage(window);
			if (mine !== generation) return;
			usage = result;
			loadError = null;
		} catch (err) {
			if (mine !== generation) return;
			usage = null;
			loadError = errorMessage(err);
		} finally {
			if (mine === generation) loading = false;
		}
	}

	$effect(() => {
		void load(days);
	});

	function selectRange(next: number) {
		if (next !== days) days = next;
	}

	const allRows = $derived(usage ? (tab === 'servers' ? serverRows(usage) : toolRows(usage)) : []);
	const rows = $derived(applyView(allRows, view));
	const toolsKnown = $derived(usage?.servers.reduce((n, s) => n + s.tools_known, 0) ?? 0);
	const toolsCalled = $derived(usage?.servers.reduce((n, s) => n + s.tools_called, 0) ?? 0);

	function formatLastCall(iso: string | null | undefined): string {
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

	const TAB_CLASS =
		'flex-1 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50';

	function pillStyle(active: boolean): string {
		return active
			? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 10%, transparent); color: var(--color-ink);'
			: 'border-color: var(--color-line); background-color: var(--color-surface-2); color: var(--color-ink-muted);';
	}
</script>

<svelte:head>
	<title>Usage · mcpelevator</title>
</svelte:head>

<section class="mx-auto flex w-full max-w-5xl flex-col gap-6">
	<div class="flex flex-wrap items-center justify-between gap-3">
		<div class="flex flex-col gap-1">
			<h1 class="text-lg font-semibold text-[var(--color-ink)]">Usage</h1>
			<p class="text-xs text-[var(--color-ink-dim)]">
				Calls served across every server — and every tool nothing has called.
			</p>
		</div>
		<div class="flex items-center gap-1" role="group" aria-label="Usage window">
			{#each RANGES as range (range.days)}
				<button
					type="button"
					onclick={() => selectRange(range.days)}
					aria-pressed={days === range.days}
					class="rounded-lg border px-2.5 py-1 text-xs font-medium transition"
					style={pillStyle(days === range.days)}
				>
					{range.label}
				</button>
			{/each}
		</div>
	</div>

	{#if loadError}
		<p
			class="rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5 text-xs text-[var(--color-state-failed)]"
			role="alert"
		>
			{loadError}
		</p>
	{:else if !usage}
		<p
			class="rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5 text-xs text-[var(--color-ink-dim)]"
		>
			Loading usage…
		</p>
	{:else}
		<!-- Totals -->
		<div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
			{#snippet tile(value: string, caption: string)}
				<div
					class="flex flex-col gap-1 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-3"
				>
					<span class="font-mono text-xl text-[var(--color-ink)]">{value}</span>
					<span class="text-[11px] text-[var(--color-ink-dim)]">{caption}</span>
				</div>
			{/snippet}
			{@render tile(String(usage.tool_calls), 'tool calls')}
			{@render tile(String(usage.other_requests), 'other requests')}
			{@render tile(`${usage.active_servers}/${usage.servers.length}`, 'servers with traffic')}
			{@render tile(`${toolsCalled}/${toolsKnown}`, 'tools called')}
		</div>

		<!-- Series -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<div class="flex flex-wrap items-center justify-between gap-2">
				<div class="flex flex-wrap items-center gap-3 text-[11px] text-[var(--color-ink-dim)]">
					{#if chartMode === 'servers'}
						<span>Tool calls per server, on one shared scale</span>
					{:else}
						<span class="flex items-center gap-1.5">
							<span
								class="size-2 rounded-[2px]"
								style="background-color: var(--color-accent);"
								aria-hidden="true"
							></span>
							tool calls
						</span>
						<span class="flex items-center gap-1.5">
							<span
								class="size-2 rounded-[2px]"
								style="background-color: color-mix(in oklab, var(--color-ink-dim) 35%, transparent);"
								aria-hidden="true"
							></span>
							other requests
						</span>
					{/if}
					<span>Last call {formatLastCall(usage.last_call_at)}</span>
				</div>
				<div class="flex items-center gap-1" role="group" aria-label="Chart style">
					{#each CHART_MODES as option (option.value)}
						<button
							type="button"
							onclick={() => (chartMode = option.value as 'bars' | 'line' | 'servers')}
							aria-pressed={chartMode === option.value}
							class="rounded-lg border px-2.5 py-1 text-xs font-medium transition"
							style={pillStyle(chartMode === option.value)}
						>
							{option.label}
						</button>
					{/each}
				</div>
			</div>
			{#if chartMode === 'servers'}
				{#if usage.series_by_server.length}
					<UsageSparklines
						series={usage.series}
						bands={usage.series_by_server}
						bucketSeconds={usage.bucket_seconds}
					/>
				{:else}
					<p class="py-8 text-center text-xs text-[var(--color-ink-dim)]">
						No tool calls in this window to split by server.
					</p>
				{/if}
			{:else}
				<UsageChart
					series={usage.series}
					bucketSeconds={usage.bucket_seconds}
					mode={chartMode}
					height="h-32"
				/>
			{/if}
		</div>

		<!-- When it's used -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<div class="flex flex-wrap items-baseline justify-between gap-2">
				<h2 class="text-sm font-semibold text-[var(--color-ink)]">Activity by hour</h2>
				<span class="text-[11px] text-[var(--color-ink-dim)]">
					tool calls per weekday and hour
				</span>
			</div>
			<UsageHeatmap hourly={usage.hourly} />
		</div>

		<!-- Breakdown -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<div class="flex w-full gap-1" role="group" aria-label="Breakdown">
				{#each [{ value: 'tools', label: 'Tools' }, { value: 'servers', label: 'Servers' }] as option (option.value)}
					<button
						type="button"
						onclick={() => (tab = option.value as 'servers' | 'tools')}
						aria-pressed={tab === option.value}
						class={TAB_CLASS}
						style={pillStyle(tab === option.value)}
					>
						{option.label}
					</button>
				{/each}
			</div>

			<div class="flex flex-wrap items-center gap-2">
				<input
					type="search"
					bind:value={view.search}
					placeholder={tab === 'tools' ? 'Filter tools…' : 'Filter servers…'}
					aria-label={tab === 'tools' ? 'Filter tools' : 'Filter servers'}
					autocomplete="off"
					spellcheck="false"
					class="min-w-40 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
				/>
				<select
					bind:value={view.sort}
					aria-label="Sort by"
					class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2 py-1.5 text-xs text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
				>
					{#each USAGE_SORTS as option (option.value)}
						<option value={option.value as UsageSort}>{option.label}</option>
					{/each}
				</select>
				<label
					class="flex cursor-pointer items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 text-xs text-[var(--color-ink-muted)]"
				>
					<input type="checkbox" bind:checked={view.hideUnused} class="size-3.5" />
					Used only
				</label>
				<div class="flex items-center gap-1" role="group" aria-label="Listing style">
					{#each [{ value: 'table', label: 'Table' }, { value: 'bars', label: 'Bars' }] as option (option.value)}
						<button
							type="button"
							onclick={() => (rowStyle = option.value as 'table' | 'bars')}
							aria-pressed={rowStyle === option.value}
							class="rounded-lg border px-2.5 py-1.5 text-xs font-medium transition"
							style={pillStyle(rowStyle === option.value)}
						>
							{option.label}
						</button>
					{/each}
				</div>
			</div>

			<p class="text-[11px] text-[var(--color-ink-dim)]">
				{countLabel(rows.length, allRows.length, tab === 'tools' ? 'tool' : 'server')}
				{#if loading}· refreshing…{/if}
			</p>

			<UsageRows
				{rows}
				style={rowStyle}
				labelHeading={tab === 'tools' ? 'Tool' : 'Server'}
				emptyMessage={allRows.length === 0
					? tab === 'tools'
						? 'No tools have been discovered or called yet.'
						: 'No servers registered yet.'
					: 'Nothing matches this filter.'}
			/>
		</div>

		<p class="text-[11px] text-[var(--color-ink-dim)]">
			Counts only — arguments and results are never recorded. The window reaches back no
			further than the usage retention set in
			<a href="/settings" class="underline underline-offset-2">Settings</a>. A server's
			tools are listed while it is running.
		</p>
	{/if}
</section>
