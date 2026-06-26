<script lang="ts">
	import { getHealth } from '$lib/api';

	let {
		intervalMs = 5000
	}: {
		intervalMs?: number;
	} = $props();

	type Status = 'pending' | 'ok' | 'down';

	let status = $state<Status>('pending');
	let version = $state<string | null>(null);

	async function poll() {
		try {
			const health = await getHealth();
			status = health.status === 'ok' ? 'ok' : 'down';
			version = health.version ?? null;
		} catch {
			status = 'down';
			version = null;
		}
	}

	// Poll on mount and on a fixed interval; clean up on unmount.
	$effect(() => {
		poll();
		const id = setInterval(poll, intervalMs);
		return () => clearInterval(id);
	});

	const color = $derived(
		status === 'ok'
			? 'var(--color-state-running)'
			: status === 'down'
				? 'var(--color-state-failed)'
				: 'var(--color-state-stopped)'
	);

	const label = $derived(
		status === 'ok'
			? version
				? `API online · v${version}`
				: 'API online'
			: status === 'down'
				? 'API unreachable'
				: 'Checking API…'
	);
</script>

<span
	class="inline-flex items-center gap-2 text-xs text-[var(--color-ink-muted)]"
	title={label}
	aria-live="polite"
>
	<span class="relative flex size-2.5 items-center justify-center">
		{#if status === 'ok'}
			<span
				class="absolute inline-flex size-full animate-ping rounded-full opacity-60"
				style="background-color: {color};"
			></span>
		{/if}
		<span
			class="relative size-2 rounded-full"
			class:animate-pulse-dot={status === 'pending'}
			style="background-color: {color};"
		></span>
	</span>
	<span class="hidden sm:inline">{label}</span>
</span>
