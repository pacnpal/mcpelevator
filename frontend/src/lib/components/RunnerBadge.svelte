<script lang="ts">
	import { RUNNER_LABEL } from '#lib/serverFilter.js';
	import type { Runner } from '#lib/types.js';

	let { runner }: { runner: Runner } = $props();
</script>

<span
	class="inline-flex items-center rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-1.5 py-0.5 font-mono text-[11px] font-medium text-[var(--color-ink-muted)]"
>
	{RUNNER_LABEL[runner] ?? runner}
</span>
