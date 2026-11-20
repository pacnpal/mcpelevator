<script lang="ts">
	// A user's role and local-runner toggles on the Users page. A field the identity
	// provider's groups manage is disabled, with the setting that manages it as the hint:
	// the backend refuses it (409), and the next sign-in would reset it anyway.
	import type { UserInfo } from '#lib/types.js';

	let {
		user,
		disabled = false,
		onpatch
	}: {
		user: UserInfo;
		disabled?: boolean;
		onpatch: (patch: Partial<Pick<UserInfo, 'role' | 'local_runners'>>) => void;
	} = $props();

	const chip =
		'rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-not-allowed disabled:opacity-50';
	const hint = (setting: string) => `Set by the identity provider's groups at sign-in (${setting})`;
</script>

<button
	type="button"
	onclick={() => onpatch({ role: user.role === 'admin' ? 'member' : 'admin' })}
	disabled={disabled || user.managed.role}
	title={user.managed.role ? hint('oidc_admin_groups') : undefined}
	class={chip}
>
	{user.role === 'admin' ? 'Make member' : 'Make admin'}
</button>
<button
	type="button"
	onclick={() => onpatch({ local_runners: !user.local_runners })}
	disabled={disabled || user.managed.local_runners}
	title={user.managed.local_runners ? hint('oidc_local_runner_groups') : undefined}
	class={chip}
>
	{user.local_runners ? 'Revoke local runners' : 'Allow local runners'}
</button>
