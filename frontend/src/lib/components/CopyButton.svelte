<script lang="ts">
	let {
		value,
		label
	}: {
		value: string | null;
		label: string;
	} = $props();

	let copied = $state(false);
	let timer: ReturnType<typeof setTimeout> | undefined;

	const disabled = $derived(!value);

	async function copy() {
		if (!value) return;
		try {
			await navigator.clipboard.writeText(value);
		} catch {
			// Fallback for non-secure contexts where the Clipboard API is blocked.
			const ta = document.createElement('textarea');
			ta.value = value;
			ta.style.position = 'fixed';
			ta.style.opacity = '0';
			document.body.appendChild(ta);
			ta.select();
			try {
				document.execCommand('copy');
			} catch {
				/* give up silently */
			}
			ta.remove();
		}
		copied = true;
		clearTimeout(timer);
		timer = setTimeout(() => (copied = false), 1400);
	}

	$effect(() => () => clearTimeout(timer));
</script>

<button
	type="button"
	onclick={copy}
	{disabled}
	aria-label={value ? `Copy ${label} URL` : `${label} URL unavailable`}
	title={value ?? `${label} URL unavailable`}
	class="group inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition active:translate-y-px enabled:hover:border-[var(--color-line-strong)] enabled:hover:text-[var(--color-ink)] disabled:cursor-not-allowed disabled:opacity-40"
>
	{#if copied}
		<svg
			class="size-3.5 text-[var(--color-accent)]"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2.5"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M20 6 9 17l-5-5" />
		</svg>
		<span class="text-[var(--color-accent)]">Copied</span>
	{:else}
		<svg
			class="size-3.5"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
			<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
		</svg>
		<span>{label}</span>
	{/if}
</button>
