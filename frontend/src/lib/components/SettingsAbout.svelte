<script lang="ts">
	// The Settings page's About section: what this is, which build is running, and where
	// it lives. The version comes from /api/health, the same source as the footer, so it
	// names the release tag the image was built from (never a hardcoded string).
	import { errorMessage, getHealth } from '#lib/api.js';
	import type { AuthUser } from '#lib/types.js';
	import CopyButton from '#lib/components/CopyButton.svelte';
	import Logo from '#lib/components/Logo.svelte';

	let {
		user,
		baseUrl,
		serverCount
	}: {
		user: AuthUser | null;
		/** The instance's public base, e.g. https://mcp.example.com. */
		baseUrl: string;
		serverCount: number;
	} = $props();

	const REPO = 'https://github.com/pacnpal/mcpelevator';

	let version = $state<string | null>(null);
	let versionError = $state<string | null>(null);

	$effect(() => {
		getHealth()
			.then((h) => (version = h.version))
			.catch((err) => (versionError = errorMessage(err)));
	});

	const links = [
		{ href: REPO, label: 'Source code', hint: 'The repository and README' },
		{ href: `${REPO}/releases`, label: 'Release notes', hint: 'What changed in each version' },
		{ href: `${REPO}/issues/new`, label: 'Report an issue', hint: 'Bugs and feature requests' },
		{ href: `${REPO}/blob/main/LICENSE`, label: 'License', hint: 'MIT' }
	];
</script>

<div class="flex flex-col gap-5">
	<div
		class="flex flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5 sm:flex-row sm:items-center"
	>
		<div class="flex items-center gap-3">
			<Logo />
		</div>
		<p class="max-w-prose text-sm text-[var(--color-ink-muted)] sm:ml-2">
			A self-hosted control plane that runs MCP servers, stdio or already remote, and serves
			each one as an authenticated Streamable-HTTP endpoint.
		</p>
	</div>

	<dl
		class="grid gap-x-6 gap-y-4 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5 sm:grid-cols-2"
	>
		<div class="flex flex-col gap-1">
			<dt class="text-xs font-medium text-[var(--color-ink-dim)]">Version</dt>
			<dd class="font-mono text-sm text-[var(--color-ink)]">
				{#if version}
					v{version}
				{:else if versionError}
					<span class="text-[var(--color-ink-dim)]" title={versionError}>unknown</span>
				{:else}
					<span class="text-[var(--color-ink-dim)]">…</span>
				{/if}
			</dd>
		</div>
		<div class="flex flex-col gap-1">
			<dt class="text-xs font-medium text-[var(--color-ink-dim)]">Signed in as</dt>
			<dd class="flex min-w-0 items-center gap-2 text-sm text-[var(--color-ink)]">
				<span class="truncate">{user?.name ?? 'local admin'}</span>
				{#if user}
					<span
						class="shrink-0 rounded-full border border-[var(--color-line)] px-2 py-0.5 text-[11px] font-medium text-[var(--color-ink-muted)]"
						>{user.role}</span
					>
				{/if}
			</dd>
		</div>
		<div class="flex min-w-0 flex-col gap-1 sm:col-span-2">
			<dt class="text-xs font-medium text-[var(--color-ink-dim)]">Instance URL</dt>
			<dd class="flex min-w-0 items-center gap-2">
				<code
					class="min-w-0 flex-1 truncate rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-xs text-[var(--color-ink)]"
					title={baseUrl}>{baseUrl}</code
				>
				<CopyButton value={baseUrl} label="Copy instance URL" text="Copy" />
			</dd>
		</div>
		<div class="flex flex-col gap-1">
			<dt class="text-xs font-medium text-[var(--color-ink-dim)]">Servers you can see</dt>
			<dd class="font-mono text-sm text-[var(--color-ink)]">{serverCount}</dd>
		</div>
	</dl>

	<ul class="grid gap-2 sm:grid-cols-2">
		{#each links as link (link.href)}
			<li>
				<a
					href={link.href}
					target="_blank"
					rel="noopener noreferrer"
					class="flex h-full flex-col gap-0.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-3 transition hover:border-[var(--color-line-strong)]"
				>
					<span class="flex items-center gap-1.5 text-sm font-medium text-[var(--color-ink)]">
						{link.label}
						<svg
							class="size-3 text-[var(--color-ink-dim)]"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="M7 17 17 7M8 7h9v9" />
						</svg>
					</span>
					<span class="text-xs text-[var(--color-ink-dim)]">{link.hint}</span>
				</a>
			</li>
		{/each}
	</ul>
</div>
