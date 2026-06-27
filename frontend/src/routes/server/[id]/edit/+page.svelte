<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { errorMessage, getServer, updateServer } from '$lib/api';
	import type { ServerCreate, ServerDetail, ServerUpdate } from '$lib/types';
	import ServerForm from '$lib/components/ServerForm.svelte';

	type LoadState = 'loading' | 'ready' | 'error';

	const id = $derived(page.params.id ?? '');

	let server = $state<ServerDetail | null>(null);
	let loadState = $state<LoadState>('loading');
	let loadError = $state<string | null>(null);

	let saving = $state(false);
	let saveError = $state<string | null>(null);

	async function load() {
		loadState = 'loading';
		try {
			server = await getServer(id);
			loadState = 'ready';
		} catch (err) {
			loadState = 'error';
			loadError = errorMessage(err);
		}
	}

	// Map the detail response into the form's initial (ServerCreate-shaped) values.
	const initial = $derived<(Partial<ServerCreate> & { slug?: string }) | null>(
		server
			? {
					name: server.name,
					slug: server.slug,
					runner: server.runner,
					command: server.command,
					args: server.args,
					env: server.env,
					cwd: server.cwd,
					mcp_http: server.transports.mcp_http,
					rest_openapi: server.transports.rest_openapi,
					auth_provider: server.auth_provider
				}
			: null
	);

	async function handleSave(payload: ServerCreate & { slug?: string }) {
		if (!server || saving) return;
		saving = true;
		saveError = null;
		// PATCH accepts any subset of create fields except `enabled` (plus an optional
		// `slug` rename). The form in edit mode never emits `enabled`, but strip it
		// defensively.
		const { enabled: _enabled, ...rest } = payload;
		void _enabled;
		const body: ServerUpdate = rest;
		try {
			await updateServer(server.id, body);
			await goto(`/server/${server.id}`);
		} catch (err) {
			saveError = errorMessage(err);
			saving = false;
		}
	}

	$effect(() => {
		void id;
		load();
	});
</script>

<section class="mx-auto flex w-full max-w-2xl flex-col gap-7">
	<!-- Back -->
	<a
		href={`/server/${id}`}
		class="inline-flex items-center gap-1.5 self-start text-sm text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
	>
		<svg
			class="size-4"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<path d="M19 12H5M12 19l-7-7 7-7" />
		</svg>
		Back to server
	</a>

	{#if loadState === 'loading'}
		<div
			class="flex items-center justify-center gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] px-6 py-20 text-sm text-[var(--color-ink-muted)]"
		>
			<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
				<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
				<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
			</svg>
			Loading…
		</div>
	{:else if loadState === 'error'}
		<div
			class="flex flex-col items-center gap-4 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] bg-[var(--color-surface)] px-6 py-16 text-center"
		>
			<p class="text-base font-semibold text-[var(--color-ink)]">Couldn't load this server</p>
			<p class="max-w-sm font-mono text-xs text-[var(--color-state-failed)]">{loadError}</p>
			<button
				type="button"
				onclick={load}
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)]"
			>
				Retry
			</button>
		</div>
	{:else if server && initial}
		{@const sid = server.id}
		<div>
			<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">
				Edit {server.name}
			</h1>
			<p class="mt-1 text-sm text-[var(--color-ink-muted)]">
				Saving changes restarts the server.
			</p>
		</div>

		<ServerForm
			mode="edit"
			{initial}
			busy={saving}
			error={saveError}
			submitLabel="Save changes"
			onsubmit={handleSave}
			oncancel={() => goto(`/server/${sid}`)}
		/>
	{/if}
</section>
