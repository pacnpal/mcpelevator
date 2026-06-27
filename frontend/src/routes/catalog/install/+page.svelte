<script lang="ts">
	import { goto } from '$app/navigation';
	import { createServer, errorMessage } from '$lib/api';
	import { takePendingInstall, type PendingInstall } from '$lib/catalogInstall';
	import ServerForm from '$lib/components/ServerForm.svelte';
	import type { ServerCreate } from '$lib/types';

	// Single-use hand-off from /catalog. On a hard refresh (or direct nav) there's no
	// pending install, so bounce back to the browser rather than render an empty form.
	let pending = $state<PendingInstall | null>(null);
	$effect(() => {
		const p = takePendingInstall();
		if (!p) {
			goto('/catalog');
			return;
		}
		pending = p;
	});

	let creating = $state(false);
	let createError = $state<string | null>(null);

	async function handleCreate(payload: ServerCreate) {
		if (creating || !pending) return;
		creating = true;
		createError = null;
		try {
			const created = await createServer({ ...payload, source: pending.source });
			await goto(`/server/${created.id}`);
		} catch (err) {
			createError = errorMessage(err);
			creating = false;
		}
	}
</script>

<section class="mx-auto flex w-full max-w-2xl flex-col gap-7">
	<!-- Back -->
	<a
		href="/catalog"
		class="inline-flex items-center gap-1.5 self-start text-sm text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
	>
		<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
			<path d="M19 12H5M12 19l-7-7 7-7" />
		</svg>
		Back to catalog
	</a>

	{#if pending}
		<!-- Heading -->
		<div>
			<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">
				Install from {pending.sourceLabel}
			</h1>
			<p class="mt-1 text-sm text-[var(--color-ink-muted)]">
				{#if pending.installSupport === 'manual'}
					This directory has no launch command — set the runner and package below, then review.
				{:else}
					Review the resolved launch config, fill in any required values, then create.
				{/if}
			</p>
		</div>

		<!-- Manual / repo notes -->
		{#if pending.notes.length || pending.repositoryUrl || pending.webUrl}
			<div class="flex flex-col gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3.5 py-3 text-xs text-[var(--color-ink-muted)]">
				{#each pending.notes as note (note)}
					<p>{note}</p>
				{/each}
				<div class="flex flex-wrap gap-3 pt-0.5">
					{#if pending.repositoryUrl}
						<a href={pending.repositoryUrl} target="_blank" rel="noreferrer noopener" class="font-medium text-[var(--color-accent)] transition hover:text-[var(--color-accent-strong)]">
							Repository ↗
						</a>
					{/if}
					{#if pending.webUrl}
						<a href={pending.webUrl} target="_blank" rel="noreferrer noopener" class="font-medium text-[var(--color-accent)] transition hover:text-[var(--color-accent-strong)]">
							Directory page ↗
						</a>
					{/if}
				</div>
			</div>
		{/if}

		<!-- Required-value warnings from the mapping -->
		{#if pending.warnings.length}
			<div
				class="flex flex-col gap-1.5 rounded-lg border px-3.5 py-3 text-xs leading-relaxed"
				style="border-color: color-mix(in oklab, var(--color-accent) 30%, transparent); background-color: color-mix(in oklab, var(--color-accent) 7%, transparent); color: var(--color-ink-muted);"
			>
				<p class="font-medium text-[var(--color-ink)]">Before you start, fill these in below:</p>
				<ul class="flex list-disc flex-col gap-1 pl-4">
					{#each pending.warnings as w (w)}
						<li>{w}</li>
					{/each}
				</ul>
			</div>
		{/if}

		<ServerForm
			mode="create"
			initial={pending.initial}
			busy={creating}
			error={createError}
			submitLabel="Install server"
			onsubmit={handleCreate}
			oncancel={() => goto('/catalog')}
		/>
	{/if}
</section>
