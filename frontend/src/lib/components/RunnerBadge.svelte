<script lang="ts">
	import type { Runner } from '$lib/types';

	let { runner }: { runner: Runner } = $props();

	const LABEL: Record<Runner, string> = {
		npx: 'npx',
		uvx: 'uvx',
		command: 'cmd',
		docker: 'docker',
		remote: 'remote'
	};
</script>

<span
	class="inline-flex items-center rounded-md border border-[var(--color-line)] bg-[var(--color-surface-2)] px-1.5 py-0.5 font-mono text-[11px] font-medium text-[var(--color-ink-muted)]"
>
	{LABEL[runner] ?? runner}
</span>
