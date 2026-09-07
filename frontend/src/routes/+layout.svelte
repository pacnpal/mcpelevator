<script lang="ts">
	import '../app.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { getAuthStatus, getHealth } from '$lib/api';
	import { clearToken } from '$lib/auth';
	import { completeOauthPopup } from '$lib/oauthPopup';
	import favicon from '$lib/assets/favicon.svg';
	import HealthDot from '$lib/components/HealthDot.svelte';
	import Logo from '$lib/components/Logo.svelte';
	import Toast from '$lib/components/Toast.svelte';
	import { toast, dismissToast } from '$lib/toast.svelte';

	let { children } = $props();

	// The OAuth callback can bounce to any page (`/server/{id}?oauth=connected` on
	// success, `/?oauth=error` on failure), so the popup hand-off lives here in the
	// root layout: when this document is the sign-in popup, broadcast the result to
	// the opener tab and close. No-op for regular tabs.
	$effect(() => {
		void page.url.search;
		completeOauthPopup(page.url);
	});

	// The running version, shown in the footer. Fetched once: it only changes when
	// the control plane restarts, which reloads the page anyway.
	let version = $state<string | null>(null);
	$effect(() => {
		getHealth()
			.then((health) => {
				version = health.version ?? null;
			})
			.catch(() => {
				// best-effort; the footer just omits the version.
			});
	});

	const onSettings = $derived(page.url.pathname.startsWith('/settings'));
	const onCatalog = $derived(page.url.pathname.startsWith('/catalog'));
	const onUsage = $derived(page.url.pathname.startsWith('/usage'));

	// Auth guard: when the control plane enforces auth and this client isn't
	// authenticated, bounce to /login. Re-runs on navigation (page.url is reactive);
	// /api/auth/status is public, so this never loops.
	let loggedIn = $state(false);
	$effect(() => {
		if (page.url.pathname === '/login') {
			loggedIn = false; // on the login page there's no session yet, so hide "Log out"
			return;
		}
		let cancelled = false;
		getAuthStatus()
			.then((status) => {
				if (cancelled) return; // a newer navigation superseded this check
				loggedIn = status.authenticated;
				if (status.enforced && !status.authenticated) goto('/login');
			})
			.catch(() => {
				// status is public and best-effort; a transient failure shouldn't trap the user.
			});
		return () => {
			cancelled = true;
		};
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
					href="/catalog"
					aria-label="Browse the registry"
					aria-current={onCatalog ? 'page' : undefined}
					class="inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-sm font-medium transition active:translate-y-px"
					style={onCatalog
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
						<circle cx="11" cy="11" r="8" />
						<path d="m21 21-4.3-4.3" />
					</svg>
					<span class="hidden sm:inline">Browse</span>
				</a>
				<a
					href="/usage"
					aria-label="Usage"
					aria-current={onUsage ? 'page' : undefined}
					class="inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-sm font-medium transition active:translate-y-px"
					style={onUsage
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
						<path d="M3 3v18h18" />
						<rect x="7" y="12" width="3" height="6" />
						<rect x="12" y="8" width="3" height="10" />
						<rect x="17" y="4" width="3" height="14" />
					</svg>
					<span class="hidden sm:inline">Usage</span>
				</a>
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

	<footer class="border-t border-[var(--color-line)]">
		<div class="mx-auto flex w-full max-w-6xl items-center justify-center gap-2 px-4 py-5 sm:px-6">
			<a
				href="https://github.com/pacnpal/mcpelevator"
				target="_blank"
				rel="noopener noreferrer"
				aria-label="mcpelevator on GitHub"
				class="inline-flex items-center gap-2 rounded-lg text-sm text-[var(--color-ink-muted)] outline-offset-4 transition hover:text-[var(--color-ink)]"
			>
				<svg class="size-5" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
					<path
						d="M8 0C3.58 0 0 3.58 0 8a8 8 0 0 0 5.47 7.59c.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8 8 0 0 0 16 8c0-4.42-3.58-8-8-8Z"
					/>
				</svg>
				<span>GitHub</span>
			</a>
			{#if version}
				<span class="text-sm text-[var(--color-ink-muted)]" aria-hidden="true">·</span>
				<span class="text-sm text-[var(--color-ink-muted)]">v{version}</span>
			{/if}
		</div>
	</footer>
</div>

{#if toast.current}
	<div
		class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:justify-end sm:px-6"
	>
		<div class="w-full max-w-sm">
			<Toast message={toast.current.message} tone={toast.current.tone} onclose={dismissToast} />
		</div>
	</div>
{/if}
