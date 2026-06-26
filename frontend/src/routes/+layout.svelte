<script lang="ts">
	import '../app.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { getAuthStatus } from '$lib/api';
	import { clearToken } from '$lib/auth';
	import favicon from '$lib/assets/favicon.svg';
	import HealthDot from '$lib/components/HealthDot.svelte';
	import Logo from '$lib/components/Logo.svelte';

	let { children } = $props();

	const onSettings = $derived(page.url.pathname.startsWith('/settings'));

	// Auth guard: when the control plane enforces auth and this client isn't
	// authenticated, bounce to /login. Re-runs on navigation (page.url is reactive);
	// /api/auth/status is public, so this never loops.
	let loggedIn = $state(false);
	$effect(() => {
		if (page.url.pathname === '/login') {
			loggedIn = false; // on the login page there's no session yet — hide "Log out"
			return;
		}
		getAuthStatus()
			.then((status) => {
				loggedIn = status.authenticated;
				if (status.enforced && !status.authenticated) goto('/login');
			})
			.catch(() => {
				// status is public and best-effort; a transient failure shouldn't trap the user.
			});
	});

	function logout() {
		clearToken();
		loggedIn = false;
		goto('/login');
	}
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>mcpelevator</title>
</svelte:head>

<div class="flex min-h-[100dvh] flex-col">
	<header
		class="sticky top-0 z-40 border-b border-[var(--color-line)] bg-[color-mix(in_oklab,var(--color-base)_85%,transparent)] backdrop-blur-md"
	>
		<div
			class="mx-auto flex h-14 w-full max-w-6xl items-center justify-between gap-4 px-4 sm:px-6"
		>
			<a
				href="/"
				class="rounded-lg outline-offset-4 transition-opacity hover:opacity-90"
				aria-label="mcpelevator home"
			>
				<Logo />
			</a>
			<div class="flex items-center gap-3 sm:gap-4">
				<a
					href="/settings"
					aria-label="Settings"
					aria-current={onSettings ? 'page' : undefined}
					class="inline-flex size-9 items-center justify-center rounded-lg border transition active:translate-y-px"
					style={onSettings
						? 'border-color: color-mix(in oklab, var(--color-accent) 40%, transparent); color: var(--color-accent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
						: 'border-color: var(--color-line); color: var(--color-ink-muted); background-color: var(--color-surface);'}
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
						<circle cx="12" cy="12" r="3" />
						<path
							d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"
						/>
					</svg>
				</a>
{#if loggedIn}
						<button
							type="button"
							onclick={logout}
							aria-label="Log out"
							class="inline-flex size-9 items-center justify-center rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] text-[var(--color-ink-muted)] transition active:translate-y-px hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
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
								<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
								<polyline points="16 17 21 12 16 7" />
								<line x1="21" x2="9" y1="12" y2="12" />
							</svg>
						</button>
					{/if}
					<HealthDot />
			</div>
		</div>
	</header>

	<main class="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 sm:py-10">
		{@render children()}
	</main>
</div>
