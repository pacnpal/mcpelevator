<script lang="ts">
	import { installOptions, INSTALL_GROUP_ORDER, type InstallOption } from '$lib/install';
	import type { ServerSummary } from '$lib/types';

	let { server }: { server: ServerSummary } = $props();

	const options = $derived(installOptions(server));
	// Bucket options by group, preserving the canonical group order and dropping
	// any group with no options for this server.
	const groups = $derived(
		INSTALL_GROUP_ORDER.map((group) => ({
			group,
			items: options.filter((o) => o.group === group)
		})).filter((g) => g.items.length > 0)
	);

	let open = $state(false);
	let copied = $state<string | null>(null); // label of the last-copied option
	let copiedTimer: ReturnType<typeof setTimeout> | undefined;
	let root = $state<HTMLElement>();

	async function writeClipboard(text: string): Promise<boolean> {
		try {
			if (navigator.clipboard?.writeText) {
				await navigator.clipboard.writeText(text);
				return true;
			}
		} catch {
			// fall through to the legacy path (non-secure contexts)
		}
		try {
			const ta = document.createElement('textarea');
			ta.value = text;
			ta.style.position = 'fixed';
			ta.style.opacity = '0';
			document.body.appendChild(ta);
			try {
				ta.select();
				return document.execCommand('copy');
			} finally {
				document.body.removeChild(ta); // always unmount, even if copy throws
			}
		} catch {
			return false;
		}
	}

	async function copy(opt: InstallOption) {
		if (!(await writeClipboard(opt.value))) return;
		copied = opt.label;
		clearTimeout(copiedTimer);
		copiedTimer = setTimeout(() => (copied = null), 1600);
	}

	// Close on outside pointerdown / Escape (defer attaching so the opening
	// click settles first).
	$effect(() => {
		if (!open) return;
		const onPointer = (e: PointerEvent) => {
			if (e.target instanceof Node && !root?.contains(e.target)) open = false;
		};
		const onKey = (e: KeyboardEvent) => {
			if (e.key === 'Escape') open = false;
		};
		const id = setTimeout(() => {
			document.addEventListener('pointerdown', onPointer);
			document.addEventListener('keydown', onKey);
		}, 0);
		return () => {
			clearTimeout(id);
			document.removeEventListener('pointerdown', onPointer);
			document.removeEventListener('keydown', onKey);
		};
	});
</script>

<div class="relative" bind:this={root}>
	<button
		type="button"
		onclick={() => (open = !open)}
		disabled={options.length === 0}
		aria-haspopup="true"
		aria-expanded={open}
		aria-label="Copy install snippet"
		class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-2.5 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition active:translate-y-px enabled:hover:border-[var(--color-line-strong)] enabled:hover:text-[var(--color-ink)] disabled:cursor-not-allowed disabled:opacity-40"
	>
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
		<span>Copy</span>
		<svg
			class="size-3 transition-transform"
			class:rotate-180={open}
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2.5"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="m6 9 6 6 6-6" />
		</svg>
	</button>

	{#if open}
		<!-- Opens upward (the card footer sits at the bottom of the card). -->
		<div
			class="absolute bottom-full left-0 z-30 mb-1.5 max-h-[min(70vh,28rem)] w-72 overflow-y-auto rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-elevated)] py-1 shadow-2xl"
		>
			<p
				class="px-3 pt-1.5 pb-1 text-[10px] font-semibold tracking-wider text-[var(--color-ink-dim)] uppercase"
			>
				Add to client
			</p>
			{#each groups as { group, items } (group)}
				<p
					class="mt-1 px-3 pt-1.5 pb-0.5 text-[10px] font-semibold tracking-wider text-[var(--color-ink-dim)] uppercase"
				>
					{group}
				</p>
				{#each items as opt (opt.label)}
					<button
						type="button"
						onclick={() => copy(opt)}
						class="flex w-full items-start justify-between gap-3 px-3 py-2 text-left text-sm text-[var(--color-ink-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
					>
						<span class="flex min-w-0 items-start gap-2">
							<span
								class="mt-0.5 inline-block w-8 shrink-0 font-mono text-[10px] tracking-wide text-[var(--color-ink-dim)] uppercase"
							>
								{opt.kind}
							</span>
							<span class="min-w-0">
								<span class="block truncate">{opt.label}</span>
								{#if opt.hint}
									<span class="block truncate text-[11px] text-[var(--color-ink-dim)]">{opt.hint}</span>
								{/if}
							</span>
						</span>
						{#if copied === opt.label}
							<span class="mt-0.5 inline-flex shrink-0 items-center gap-1 text-xs font-medium text-[var(--color-accent)]">
								<svg
									class="size-3.5"
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
								Copied
							</span>
						{/if}
					</button>
				{/each}
			{/each}
		</div>
	{/if}
</div>
