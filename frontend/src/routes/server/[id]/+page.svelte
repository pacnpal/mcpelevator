<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import {
		cloneServer,
		deleteServer,
		disableServer,
		disconnectOauth,
		enableServer,
		errorMessage,
		getServer,
		getSettings,
		startOauth
	} from '$lib/api';
	import { listenForOauthResult, openOauthPopup, popupCanRelay } from '$lib/oauthPopup';
	import type { AuthProvider, ServerDetail } from '$lib/types';
	import CopyButton from '$lib/components/CopyButton.svelte';
	import LogViewer from '$lib/components/LogViewer.svelte';
	import RunnerBadge from '$lib/components/RunnerBadge.svelte';
	import StatePill from '$lib/components/StatePill.svelte';
	import Toast from '$lib/components/Toast.svelte';

	type LoadState = 'loading' | 'ready' | 'error';

	const id = $derived(page.params.id ?? '');

	let server = $state<ServerDetail | null>(null);
	let loadState = $state<LoadState>('loading');
	let loadError = $state<string | null>(null);

	// Global default auth, used to resolve a server set to `inherit` so the
	// endpoint hint reflects the *effective* auth. Best-effort; ignore failures.
	let defaultAuth = $state<AuthProvider | null>(null);

	let busy = $state(false); // enable/disable in flight
	let deleting = $state(false);
	let confirmDelete = $state(false);
	let cloning = $state(false);

	let toast = $state<string | null>(null);
	let toastTimer: ReturnType<typeof setTimeout> | undefined;
	function flashToast(message: string) {
		toast = message;
		clearTimeout(toastTimer);
		toastTimer = setTimeout(() => (toast = null), 6000);
	}

	async function load(silent = false) {
		// Capture the id this request is for. A clone navigates /server/[id] ->
		// /server/[id] (same route, reused component), so an in-flight request from
		// the source page (initial load or silent poll) can resolve *after* the
		// copy's load — drop it instead of clobbering `server` with the source.
		const requestedId = id;
		if (!silent) loadState = 'loading';
		try {
			const result = await getServer(requestedId);
			if (requestedId !== id) return; // route changed mid-flight; stale response
			server = result;
			loadState = 'ready';
			loadError = null;
		} catch (err) {
			if (requestedId !== id) return;
			if (!silent) {
				loadState = 'error';
				loadError = errorMessage(err);
			}
		}
	}

	const wantsRun = $derived(server?.enabled ?? false);

	async function toggle() {
		if (!server || busy) return;
		busy = true;
		// Capture the id this action targets. Clone reuses this component (same-route
		// nav), so if the route changes mid-flight the resolved summary belongs to the
		// *previous* server — drop it instead of clobbering the copy with the source.
		const requestedId = id;
		try {
			const updated = wantsRun
				? await disableServer(requestedId)
				: await enableServer(requestedId);
			if (requestedId !== id || !server) return; // route changed mid-flight
			server = { ...server, ...updated };
		} catch (err) {
			if (requestedId === id) flashToast(errorMessage(err));
		} finally {
			busy = false;
		}
	}

	async function doClone() {
		// Don't start a clone while an enable/disable or delete is still in flight —
		// the toggle response is id-guarded above, but blocking here keeps the source
		// page from kicking off conflicting actions right before it navigates away.
		if (!server || cloning || busy || deleting) return;
		// Capture the route + target id: if the user leaves this page before the
		// clone resolves, don't navigate to the copy or toast on the wrong route.
		const requestedId = id;
		const sourceId = server.id;
		cloning = true;
		try {
			const copy = await cloneServer(sourceId);
			if (requestedId !== id) return; // navigated away mid-flight
			// Land on the copy so the operator can review/edit, then enable it.
			await goto(`/server/${copy.id}`);
		} catch (err) {
			if (requestedId === id) flashToast(errorMessage(err));
		} finally {
			// Same-route nav (/server/[id] -> /server/[id]) reuses this component, and
			// an aborted goto resolves false without throwing — always clear the flag
			// so the copy's page doesn't show the Clone button stuck disabled.
			cloning = false;
		}
	}

	let oauthBusy = $state(false); // authorize / disconnect in flight
	let oauthPopupWatch: ReturnType<typeof setInterval> | undefined;
	// Nonce of the flow THIS page started; broadcasts carrying any other nonce belong
	// to a different tab's sign-in and are ignored.
	let oauthNonce: string | null = null;

	// While the popup is open, poll for the operator closing it mid-sign-in (there's no
	// event for that) so the Authorize button doesn't stay stuck busy. A completed flow
	// clears the watch via the broadcast listener before the popup closes itself.
	function watchOauthPopup(popup: Window) {
		clearInterval(oauthPopupWatch);
		oauthPopupWatch = setInterval(() => {
			if (!popup.closed) return;
			clearInterval(oauthPopupWatch);
			oauthPopupWatch = undefined;
			// `closed` is only a HINT: a provider that serves its auth pages with
			// Cross-Origin-Opener-Policy severs our WindowProxy, which then reports
			// closed=true while the popup is still open. Re-enable the button and
			// refresh, but KEEP the nonce so a completion broadcast that arrives
			// later (the operator finishing sign-in in that "closed" popup) still
			// lands instead of being dropped.
			oauthBusy = false;
			void load(true);
		}, 500);
	}

	async function startOauthFlow() {
		if (!server || oauthBusy) return;
		oauthBusy = true;
		// Capture the initiating route id: a clone or sidebar navigation reuses this
		// component (see toggle()/doClone()), and a slow discovery must not drive the
		// PREVIOUS server's sign-in from the new page.
		const requestedId = id;
		// Open the popup synchronously in the click gesture — popup blockers only allow
		// window.open there — and point it at the provider once the URL arrives. The
		// provider redirects back to /api/oauth/callback inside the popup; the root
		// layout broadcasts the result here (same-origin channel) and closes it.
		const handle = openOauthPopup();
		try {
			const { authorize_url } = await startOauth(server.id);
			if (requestedId !== id) {
				// Route changed mid-flight: abandon this flow instead of navigating the
				// popup (or worse, this tab) to the old server's provider.
				handle?.popup.close();
				return;
			}
			if (handle === null) {
				// Popup blocked: fall back to the full-page navigation; the callback
				// bounces back to this page with ?oauth=….
				window.location.href = authorize_url;
			} else if (handle.popup.closed) {
				// The operator closed the popup while the URL was being fetched —
				// that's a cancel, not a blocker; don't yank the whole tab away.
				oauthBusy = false;
			} else if (!popupCanRelay(authorize_url)) {
				// The callback lands on a DIFFERENT origin (MCPE_PUBLIC_BASE_URL set
				// while browsing via LAN/localhost): the popup could never message us
				// back, so use the full-tab flow for this configuration.
				handle.popup.close();
				window.location.href = authorize_url;
			} else {
				oauthNonce = handle.nonce;
				handle.popup.location.href = authorize_url;
				watchOauthPopup(handle.popup);
			}
		} catch (err) {
			handle?.popup.close();
			if (requestedId === id) flashToast(errorMessage(err));
			oauthBusy = false;
		}
	}

	// Receive the sign-in result broadcast from the popup by the root layout.
	$effect(() => {
		const stop = listenForOauthResult(({ result, reason, nonce }) => {
			if (oauthNonce === null || nonce !== oauthNonce) return; // another tab's flow
			oauthNonce = null;
			clearInterval(oauthPopupWatch);
			oauthPopupWatch = undefined;
			oauthBusy = false;
			if (result === 'connected') {
				flashToast('Connected — OAuth sign-in complete.');
				void load(true); // pick up the new oauth/bridge status without flicker
			} else {
				flashToast(reason ? `OAuth failed: ${reason}` : 'OAuth sign-in failed.');
			}
		});
		return () => {
			stop();
			clearInterval(oauthPopupWatch);
		};
	});

	async function doDisconnect() {
		if (!server || oauthBusy) return;
		oauthBusy = true;
		try {
			const updated = await disconnectOauth(server.id);
			server = { ...server, ...updated };
			flashToast(
				updated.enabled
					? 'Disconnected — the server was restarted and needs to re-authenticate to reconnect.'
					: 'Disconnected — re-authenticate to reconnect this server.'
			);
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			oauthBusy = false;
		}
	}

	// Surface the result of an OAuth round-trip (?oauth=connected|error&reason=…), then
	// strip the query so a refresh doesn't re-toast. Runs once per navigation.
	function consumeOauthResult() {
		const result = page.url.searchParams.get('oauth');
		if (!result) return;
		if (result === 'connected') flashToast('Connected — OAuth sign-in complete.');
		else if (result === 'error') {
			const reason = page.url.searchParams.get('reason');
			flashToast(reason ? `OAuth failed: ${reason}` : 'OAuth sign-in failed.');
		}
		void goto(`/server/${id}`, { replaceState: true, noScroll: true, keepFocus: true });
	}

	async function doDelete() {
		if (!server || deleting) return;
		deleting = true;
		try {
			await deleteServer(server.id);
			await goto('/');
		} catch (err) {
			flashToast(errorMessage(err));
			deleting = false;
			confirmDelete = false;
		}
	}

	// Initial load + lightweight polling so live state (running/starting) stays
	// fresh while the page is open. Polls silently to avoid flicker.
	// Consume the OAuth round-trip result in its OWN effect, keyed on the query string, so
	// it fires on the post-callback navigation to `/server/{id}?oauth=…` even when the
	// server id is unchanged (same page) — the load effect below only re-runs on id, and
	// would otherwise never surface the toast when you were already on this page.
	$effect(() => {
		void page.url.search;
		consumeOauthResult();
	});

	$effect(() => {
		// Re-run when the route id changes.
		void id;
		// A route change orphans any in-flight OAuth flow from the previous server —
		// stop watching its popup and drop its nonce so a late broadcast can't toast
		// or refresh on behalf of a server this page no longer shows.
		oauthNonce = null;
		clearInterval(oauthPopupWatch);
		oauthPopupWatch = undefined;
		oauthBusy = false;
		load();
		// Resolve the global default once so `inherit` servers show their
		// effective auth. Best-effort — endpoint hint just hides on failure.
		getSettings()
			.then((s) => (defaultAuth = s.default_auth_provider))
			.catch(() => {});
		const poll = setInterval(() => {
			if (loadState === 'ready' && !busy && !deleting) load(true);
		}, 4000);
		return () => {
			clearInterval(poll);
			clearTimeout(toastTimer);
		};
	});

	const envEntries = $derived(Object.entries(server?.env ?? {}));

	// OAuth banner state. `oauth` is the resolved status object on a remote server.
	const oauthState = $derived(server?.oauth_status ?? null);
	const oauthExpiry = $derived(
		oauthState?.expires_at ? new Date(oauthState.expires_at * 1000) : null
	);
	// The access token has lapsed by the clock — but that's only ALARMING when there's no
	// refresh token to renew it. With a refresh token the bridge refreshes silently on the
	// next call, so a lapsed access token is business as usual, not a red banner.
	const oauthLapsed = $derived(!!oauthExpiry && oauthExpiry.getTime() < Date.now());
	const oauthExpired = $derived(oauthLapsed && !oauthState?.has_refresh_token);

	// Effective auth for the endpoint hint: `inherit` resolves to the global
	// default. `null` while the default is still unknown for an inherit server.
	const effectiveBearer = $derived(
		server?.auth_provider === 'bearer' ||
			(server?.auth_provider === 'inherit' && defaultAuth === 'bearer')
	);

	// Render the stored command + args as a single shell-ish line, quoting any
	// token that contains whitespace so the spacing reads correctly.
	const commandLine = $derived(
		[server?.command ?? '', ...(server?.args ?? [])]
			.filter((p) => p.length > 0)
			.map((p) => (/\s/.test(p) ? `"${p}"` : p))
			.join(' ')
	);

	// Browser tab title: reflect the server being viewed (the layout otherwise leaves it a
	// constant "mcpelevator" on every server page). Surface an OAuth server that still needs
	// authenticating so the tab flags it even before you scroll to the banner.
	const pageTitle = $derived(
		server
			? `${oauthState?.needs_auth ? '⚠ ' : ''}${server.name} · mcpelevator`
			: 'mcpelevator'
	);
</script>

<svelte:head>
	<title>{pageTitle}</title>
</svelte:head>

<section class="mx-auto flex w-full max-w-3xl flex-col gap-6">
	<!-- Back -->
	<a
		href="/"
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
		Back to servers
	</a>

	{#if loadState === 'loading'}
		<div
			class="flex items-center justify-center gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] px-6 py-20 text-sm text-[var(--color-ink-muted)]"
		>
			<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
				<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
				<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
			</svg>
			Loading server…
		</div>
	{:else if loadState === 'error'}
		<div
			class="flex flex-col items-center gap-4 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] bg-[var(--color-surface)] px-6 py-16 text-center"
		>
			<p class="text-base font-semibold text-[var(--color-ink)]">
				Couldn't load this server
			</p>
			<p class="max-w-sm font-mono text-xs text-[var(--color-state-failed)]">
				{loadError}
			</p>
			<button
				type="button"
				onclick={() => load()}
				class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)]"
			>
				Retry
			</button>
		</div>
	{:else if server}
		<!-- Header -->
		<div class="flex flex-wrap items-start justify-between gap-4">
			<div class="min-w-0">
				<div class="flex items-center gap-3">
					<h1
						class="truncate text-2xl font-semibold tracking-tight text-[var(--color-ink)]"
					>
						{server.name}
					</h1>
					<StatePill state={server.state} />
				</div>
				<p class="mt-1 truncate font-mono text-sm text-[var(--color-ink-dim)]">
					{server.slug}
				</p>
			</div>

			<div class="flex items-center gap-2">
				<button
					type="button"
					onclick={toggle}
					disabled={busy}
					aria-busy={busy}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-semibold transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
					style={wantsRun
						? 'color: var(--color-ink-muted); border: 1px solid var(--color-line);'
						: 'color: var(--color-accent-ink); background-color: var(--color-accent);'}
				>
					{#if busy}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						{wantsRun ? 'Stopping' : 'Starting'}
					{:else if wantsRun}
						<svg class="size-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<rect x="7" y="7" width="10" height="10" rx="1.5" />
						</svg>
						Stop
					{:else}
						<svg class="size-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<path d="M8 5v14l11-7z" />
						</svg>
						Start
					{/if}
				</button>

				<button
					type="button"
					onclick={doClone}
					disabled={cloning || busy || deleting}
					aria-busy={cloning}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3.5 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70"
				>
					{#if cloning}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						Cloning
					{:else}
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
							<rect x="9" y="9" width="11" height="11" rx="2" />
							<path d="M5 15V5a2 2 0 0 1 2-2h10" />
						</svg>
						Clone
					{/if}
				</button>

				<a
					href={`/server/${server.id}/edit`}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3.5 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
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
						<path d="M12 20h9" />
						<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
					</svg>
					Edit
				</a>
			</div>
		</div>

		<!-- Upstream OAuth: connect / status banner -->
		{#if oauthState?.enabled}
			{#if oauthState.needs_auth}
				<!-- Not yet connected: the primary call to action. -->
				<div
					class="flex flex-col gap-3 rounded-[var(--radius-card)] border px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
					style="border-color: color-mix(in oklab, var(--color-accent) 45%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);"
				>
					<div class="flex items-start gap-3">
						<svg class="mt-0.5 size-5 shrink-0 text-[var(--color-accent)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
							<rect x="3" y="11" width="18" height="11" rx="2" />
							<path d="M7 11V7a5 5 0 0 1 10 0v4" />
						</svg>
						<div class="min-w-0">
							<p class="text-sm font-semibold text-[var(--color-ink)]">
								This server uses OAuth to authenticate
							</p>
							<p class="mt-0.5 text-xs leading-relaxed text-[var(--color-ink-muted)]">
								Sign in with the provider to connect it. mcpelevator stores the tokens and
								refreshes them automatically.
							</p>
						</div>
					</div>
					<button
						type="button"
						onclick={startOauthFlow}
						disabled={oauthBusy}
						aria-busy={oauthBusy}
						class="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-semibold text-[var(--color-on-accent,#fff)] transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
						style="background-color: var(--color-accent);"
					>
						{#if oauthBusy}
							<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
								<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
								<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
							</svg>
							Starting…
						{:else}
							Authenticate with provider
						{/if}
					</button>
				</div>
			{:else}
				<!-- Connected: quiet status + re-auth / disconnect. -->
				<div
					class="flex flex-col gap-2 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-3"
				>
					<div class="flex flex-wrap items-center justify-between gap-2">
						<div class="flex items-center gap-2">
							<svg class="size-4 shrink-0 {oauthExpired ? 'text-[var(--color-state-failed)]' : 'text-[var(--color-state-running,var(--color-accent))]'}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								{#if oauthExpired}
									<circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
								{:else}
									<path d="M20 6 9 17l-5-5" />
								{/if}
							</svg>
							<span class="text-sm font-medium text-[var(--color-ink)]">
								{oauthExpired ? 'OAuth token expired' : 'Authenticated via OAuth'}
							</span>
						</div>
						<div class="flex items-center gap-2">
							<button
								type="button"
								onclick={startOauthFlow}
								disabled={oauthBusy}
								aria-busy={oauthBusy}
								class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70"
							>
								Re-authenticate
							</button>
							<button
								type="button"
								onclick={doDisconnect}
								disabled={oauthBusy}
								class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-wait disabled:opacity-70"
							>
								Disconnect
							</button>
						</div>
					</div>
					<p class="text-xs leading-relaxed text-[var(--color-ink-dim)]">
						{#if oauthExpired}
							The access token has expired and there's no refresh token to renew it
							automatically. Re-authenticate to reconnect.
						{:else if oauthState?.has_refresh_token}
							Access token renews automatically.
						{:else if oauthExpiry}
							Access token valid until {oauthExpiry.toLocaleString()}. You'll need to
							re-authenticate here once it expires.
						{:else}
							Tokens renew automatically.
						{/if}
						OAuth sessions can lapse over time — if this server ever stops responding, re-authenticate here.
					</p>
				</div>
			{/if}
		{/if}

		<!-- last_error -->
		{#if server.last_error}
			<p
				class="rounded-lg border px-3.5 py-3 font-mono text-xs leading-relaxed"
				style="border-color: color-mix(in oklab, var(--color-state-failed) 30%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 10%, transparent); color: var(--color-state-failed);"
			>
				{server.last_error}
			</p>
		{/if}

		<!-- Endpoints -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<h2 class="text-sm font-semibold text-[var(--color-ink)]">Endpoints</h2>
			<div class="flex flex-col gap-2.5">
				<div class="flex items-center justify-between gap-3">
					<div class="min-w-0 flex-1">
						<p class="text-xs font-medium text-[var(--color-ink-muted)]">MCP</p>
						<p class="truncate font-mono text-xs text-[var(--color-ink)]">
							{server.urls.mcp ?? '— not exposed —'}
						</p>
					</div>
					<CopyButton value={server.urls.mcp} label="Copy" />
				</div>
				<!-- REST/OpenAPI endpoint omitted: the surface isn't served yet (planned, M6). -->
			</div>
			{#if effectiveBearer}
				<p
					class="flex items-center gap-1.5 border-t border-[var(--color-line)] pt-2.5 text-xs text-[var(--color-ink-dim)]"
				>
					<svg
						class="size-3.5 shrink-0 text-[var(--color-accent)]"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<rect x="5" y="11" width="14" height="10" rx="2" />
						<path d="M8 11V7a4 4 0 0 1 8 0v4" />
					</svg>
					<span>
						Requests need <code class="font-mono text-[var(--color-ink-muted)]">Authorization: Bearer &lt;token&gt;</code>.
						Manage tokens in
						<a
							href="/settings"
							class="text-[var(--color-ink-muted)] underline decoration-dotted underline-offset-2 transition hover:text-[var(--color-ink)]"
						>
							Settings
						</a>.
					</span>
				</p>
			{/if}
		</div>

		<!-- Configuration -->
		<div
			class="flex flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<h2 class="text-sm font-semibold text-[var(--color-ink)]">Configuration</h2>

			<div class="flex flex-wrap items-center gap-2">
				<RunnerBadge runner={server.runner} />
				{#if typeof server.pid === 'number'}
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">pid {server.pid}</span>
				{/if}
				{#if typeof server.port === 'number'}
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">port {server.port}</span>
				{/if}
			</div>

			<div class="flex flex-col gap-1.5">
				<span class="text-xs font-medium text-[var(--color-ink-muted)]">Command</span>
				<div
					class="overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5"
				>
					<code class="font-mono text-xs whitespace-pre text-[var(--color-ink)]">{commandLine}</code>
				</div>
			</div>

			{#if server.cwd}
				<div class="flex flex-col gap-1.5">
					<span class="text-xs font-medium text-[var(--color-ink-muted)]">Working directory</span>
					<code class="font-mono text-xs text-[var(--color-ink)]">{server.cwd}</code>
				</div>
			{/if}

			<div class="flex flex-col gap-1.5">
				<span class="text-xs font-medium text-[var(--color-ink-muted)]">Environment</span>
				{#if envEntries.length === 0}
					<p class="text-xs text-[var(--color-ink-dim)]">No environment variables.</p>
				{:else}
					<dl class="flex flex-col gap-1">
						{#each envEntries as [k, v] (k)}
							<div class="flex gap-2 font-mono text-xs">
								<dt class="shrink-0 text-[var(--color-accent)]">{k}</dt>
								<dd class="truncate text-[var(--color-ink-muted)]">{v}</dd>
							</div>
						{/each}
					</dl>
				{/if}
			</div>

			<div class="flex flex-wrap gap-x-6 gap-y-1.5 border-t border-[var(--color-line)] pt-3 text-xs">
				<span class="text-[var(--color-ink-dim)]">
					Source <span class="font-mono text-[var(--color-ink-muted)]">{server.source}</span>
				</span>
				<span class="text-[var(--color-ink-dim)]">
					Auth <span class="font-mono text-[var(--color-ink-muted)]">{server.auth_provider}</span>
				</span>
			</div>
		</div>

		<!-- Tools -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<div class="flex items-center justify-between">
				<h2 class="text-sm font-semibold text-[var(--color-ink)]">Tools</h2>
				<span class="font-mono text-xs text-[var(--color-ink-dim)]">{server.tools_count}</span>
			</div>
			{#if server.tools.length === 0}
				<p class="text-xs text-[var(--color-ink-dim)]">
					{server.state === 'running'
						? 'No tools discovered.'
						: 'Tools are discovered once the server is running.'}
				</p>
			{:else}
				<ul class="flex flex-col divide-y divide-[var(--color-line)]">
					{#each server.tools as tool (tool.name)}
						<li class="flex flex-col gap-0.5 py-2 first:pt-0 last:pb-0">
							<span class="flex flex-wrap items-center gap-1.5">
								<span class="font-mono text-xs font-medium text-[var(--color-ink)]">
									{tool.name}
								</span>
								{#if tool.has_output_schema === false}
									<!-- Mirrors the hint MCP clients show for schema-less tools. The
									     schema lives in the upstream server's tool definition and is
									     proxied through unchanged, so this is diagnostic only. -->
									<span
										class="rounded-md border border-[var(--color-line)] px-1.5 py-0.5 text-[11px] font-medium text-[var(--color-ink-dim)]"
										title="This tool doesn't declare an outputSchema. MCP clients recommend adding one so models can better understand the tool's results. It comes from the upstream server's tool definition — mcpelevator proxies schemas through unchanged, so the fix belongs upstream."
									>
										no output schema
									</span>
								{/if}
							</span>
							{#if tool.description}
								<span class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
									{tool.description}
								</span>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</div>

		<!-- Logs -->
		<LogViewer serverId={server.id} serverState={server.state} />

		<!-- Danger zone -->
		<div
			class="flex flex-col gap-3 rounded-[var(--radius-card)] border px-5 py-4"
			style="border-color: color-mix(in oklab, var(--color-state-failed) 25%, var(--color-line));"
		>
			{#if !confirmDelete}
				<div class="flex flex-wrap items-center justify-between gap-3">
					<div>
						<p class="text-sm font-medium text-[var(--color-ink)]">Delete server</p>
						<p class="text-xs text-[var(--color-ink-dim)]">
							Stops the server and removes it permanently.
						</p>
					</div>
					<button
						type="button"
						onclick={() => (confirmDelete = true)}
						class="shrink-0 rounded-lg border px-3.5 py-2 text-sm font-medium transition active:translate-y-px"
						style="border-color: color-mix(in oklab, var(--color-state-failed) 40%, transparent); color: var(--color-state-failed);"
					>
						Delete
					</button>
				</div>
			{:else}
				<div class="flex flex-col gap-3">
					<p class="text-sm text-[var(--color-ink)]">
						Delete <span class="font-semibold">{server.name}</span>? This stops and
						removes it.
					</p>
					<div class="flex items-center gap-2">
						<button
							type="button"
							onclick={doDelete}
							disabled={deleting}
							aria-busy={deleting}
							class="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
							style="background-color: var(--color-state-failed);"
						>
							{#if deleting}
								<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
									<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
									<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
								</svg>
								Deleting
							{:else}
								Yes, delete
							{/if}
						</button>
						<button
							type="button"
							onclick={() => (confirmDelete = false)}
							disabled={deleting}
							class="rounded-lg border border-[var(--color-line)] px-4 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
						>
							Cancel
						</button>
					</div>
				</div>
			{/if}
		</div>
	{/if}
</section>

<!-- Toast -->
{#if toast}
	<div
		class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:justify-end sm:px-6"
	>
		<div class="w-full max-w-sm">
			<Toast message={toast} onclose={() => (toast = null)} />
		</div>
	</div>
{/if}
