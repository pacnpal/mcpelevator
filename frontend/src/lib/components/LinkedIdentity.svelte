<script lang="ts">
	// A user's Linked identity on the Users page: a badge naming the issuer's host, and an
	// Unlink button behind a confirm. Unlinking ends the user's Sign-in sessions; their next
	// SSO sign-in matches by name again (how users move to a new identity provider).
	import { errorMessage, unlinkUserIdentity } from '#lib/api.js';
	import { flashToast } from '#lib/toast.svelte.js';

	let {
		userId,
		issuer,
		disabled = false,
		onunlinked
	}: {
		userId: string;
		issuer: string;
		disabled?: boolean;
		onunlinked: () => void;
	} = $props();

	let confirming = $state(false);
	let busy = $state(false);

	const host = $derived.by(() => {
		try {
			return new URL(issuer).host;
		} catch {
			return issuer;
		}
	});

	async function unlink() {
		if (busy) return;
		busy = true;
		try {
			await unlinkUserIdentity(userId);
			onunlinked();
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
	<span
		class="shrink-0 rounded-full border border-[var(--color-line)] px-2 py-0.5 text-[11px] font-medium whitespace-nowrap text-[var(--color-ink-muted)]"
		title="Signs in through SSO as {issuer}"
	>
		linked · {host}
	</span>
	{#if confirming}
		<button
			type="button"
			onclick={unlink}
			disabled={busy}
			aria-busy={busy}
			class="rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
			style="background-color: var(--color-state-failed);"
		>
			Confirm unlink
		</button>
		<button type="button" onclick={() => (confirming = false)} disabled={busy} class={chip}>
			Cancel
		</button>
	{:else}
		<button
			type="button"
			onclick={() => (confirming = true)}
			{disabled}
			title="Clear this link and end the user's SSO sessions; their next sign-in matches by name"
			class={chip}
		>
			Unlink
		</button>
	{/if}
</span>
