<script lang="ts">
	// A user's active state on the Users page: a deactivated badge, and Deactivate or
	// Reactivate behind a confirm. Deactivating ends the user's Sign-in sessions and
	// revokes every token they hold; their servers keep running. Reactivating restores
	// sign-in, and revoked tokens stay revoked.
	import { errorMessage, updateUser } from '#lib/api.js';
	import { flashToast } from '#lib/toast.svelte.js';
	import type { UserInfo } from '#lib/types.js';

	let {
		userId,
		active,
		disabled = false,
		onchanged
	}: {
		userId: string;
		active: boolean;
		disabled?: boolean;
		onchanged: (user: UserInfo) => void;
	} = $props();

	let confirming = $state(false);
	let busy = $state(false);
	const verb = $derived(active ? 'deactivate' : 'reactivate');

	async function apply() {
		if (busy) return;
		busy = true;
		try {
			onchanged(await updateUser(userId, { active: !active }));
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			busy = false;
			confirming = false;
		}
	}

	const chip =
		'rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-50';
</script>

<span class="flex flex-wrap items-center gap-1.5">
	{#if !active}
		<span
			class="shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap"
			style="border-color: var(--color-state-failed); color: var(--color-state-failed);"
			title="Can't sign in, and holds no working token. Their servers keep running."
		>
			deactivated
		</span>
	{/if}
	{#if confirming}
		<button
			type="button"
			onclick={apply}
			disabled={busy}
			aria-busy={busy}
			class="rounded-lg px-3 py-1.5 text-xs font-semibold transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
			style={active
				? 'background-color: var(--color-state-failed); color: white;'
				: 'background-color: var(--color-accent); color: var(--color-accent-ink);'}
		>
			Confirm {verb}
		</button>
		<button type="button" onclick={() => (confirming = false)} disabled={busy} class={chip}>
			Cancel
		</button>
	{:else}
		<button
			type="button"
			onclick={() => (confirming = true)}
			{disabled}
			title={active
				? "End this user's sessions and revoke every token they hold; their servers keep running"
				: 'Let this user sign in again; revoked tokens stay revoked'}
			class={chip}
		>
			{active ? 'Deactivate' : 'Reactivate'}
		</button>
	{/if}
</span>
