<script lang="ts">
	import { startupPhaseLabel } from '$lib/startup';
	import type { ServerState, StartupStatus } from '$lib/types';

	let {
		state,
		startupStatus = null
	}: { state: ServerState; startupStatus?: StartupStatus | null } = $props();

	const META: Record<
		ServerState,
		{ label: string; color: string; pulse: boolean }
	> = {
		running: { label: 'Running', color: 'var(--color-state-running)', pulse: false },
		starting: { label: 'Starting', color: 'var(--color-state-starting)', pulse: true },
		stopping: { label: 'Stopping', color: 'var(--color-state-starting)', pulse: true },
		unhealthy: { label: 'Unhealthy', color: 'var(--color-state-unhealthy)', pulse: false },
		failed: { label: 'Failed', color: 'var(--color-state-failed)', pulse: false },
		stopped: { label: 'Stopped', color: 'var(--color-state-stopped)', pulse: false },
		// Deliberately asleep, wakes on the next request — calm (accent-ish), not alarming.
		idle: { label: 'Idle', color: 'var(--color-state-idle, var(--color-state-stopped))', pulse: false }
	};

	const meta = $derived(
		startupStatus
			? {
					label: startupPhaseLabel(startupStatus.phase),
					color: 'var(--color-state-starting)',
					pulse: true
				}
			: (META[state] ?? META.stopped)
	);
</script>

<span
	class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium tracking-wide"
	style="color: {meta.color}; border-color: color-mix(in oklab, {meta.color} 35%, transparent); background-color: color-mix(in oklab, {meta.color} 12%, transparent);"
>
	<span
		class="size-1.5 rounded-full"
		class:animate-pulse-dot={meta.pulse}
		style="background-color: {meta.color};"
	></span>
	{meta.label}
</span>
