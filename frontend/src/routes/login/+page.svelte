<script lang="ts">
	import { goto } from '$app/navigation';
	import { errorMessage, getAuthStatus } from '$lib/api';
	import { clearToken, setToken } from '$lib/auth';

	let token = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	const valid = $derived(token.trim().length > 0);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (busy || !valid) return;
		busy = true;
		error = null;
		// Store it, then ask the backend whether it's a valid control token. Clear it
		// again if not, so a bad value doesn't linger and 401 every later request.
		setToken(token.trim());
		try {
			const status = await getAuthStatus();
			if (status.authenticated) {
				await goto('/');
			} else {
				clearToken();
				error = 'That token was not accepted. Check the value and try again.';
			}
		} catch (err) {
			clearToken();
			error = errorMessage(err);
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head>
	<title>Sign in · mcpelevator</title>
</svelte:head>

<section class="mx-auto flex w-full max-w-md flex-col gap-6 py-8">
	<div class="flex flex-col gap-2">
		<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">Admin sign-in</h1>
		<p class="text-sm text-[var(--color-ink-muted)]">
			Control-plane access is protected. Paste the admin token printed in the server logs at
			startup (look for <span class="font-mono text-xs">control-plane auth is ON</span>), or the
			value of <code class="rounded bg-[var(--color-surface-2)] px-1 py-0.5 font-mono text-xs"
				>MCPE_ADMIN_TOKEN</code
			>.
		</p>
	</div>

	<form
		onsubmit={submit}
		class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
	>
		<label for="admin-token" class="text-xs font-medium text-[var(--color-ink-muted)]">
			Admin token
		</label>
		<input
			id="admin-token"
			type="password"
			bind:value={token}
			autocomplete="off"
			spellcheck="false"
			placeholder="mcpe_…"
			class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
		/>
		{#if error}
			<p class="text-xs" style="color: var(--color-state-failed);">{error}</p>
		{/if}
		<button
			type="submit"
			disabled={!valid || busy}
			aria-busy={busy}
			class="inline-flex items-center justify-center gap-2 rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)] disabled:cursor-not-allowed disabled:opacity-50"
		>
			{#if busy}
				<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
					<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
					<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
				</svg>
			{/if}
			Sign in
		</button>
	</form>
</section>
