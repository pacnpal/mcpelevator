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
		retryServer,
		startOauth,
		updateServer
	} from '$lib/api';
	import { listenForOauthResult, openOauthPopup, popupCanRelay } from '$lib/oauthPopup';
	import {
		formatCountdown,
		formatElapsed,
		hasActiveStartup,
		pollingInterval,
		primaryServerAction,
		startupPhaseLabel
	} from '$lib/startup';
	import type { ServerDetail, ServerTool } from '$lib/types';
	import CopyButton from '$lib/components/CopyButton.svelte';
	import LogViewer from '$lib/components/LogViewer.svelte';
	import RunnerBadge from '$lib/components/RunnerBadge.svelte';
	import StatePill from '$lib/components/StatePill.svelte';
	import ToolRunner from '$lib/components/ToolRunner.svelte';
	import { flashToast } from '$lib/toast.svelte';

	type LoadState = 'loading' | 'ready' | 'error';

	const id = $derived(page.params.id ?? '');

	let server = $state<ServerDetail | null>(null);
	let loadState = $state<LoadState>('loading');
	let loadError = $state<string | null>(null);

	let busy = $state(false); // start/stop/retry in flight
	let deleting = $state(false);
	let confirmDelete = $state(false);
	let cloning = $state(false);

	let loadingId: string | null = null;
	let mutationRevision = 0;

	async function load(silent = false) {
		// Capture the id this request is for. A clone navigates /server/[id] ->
		// /server/[id] (same route, reused component), so an in-flight request from
		// the source page (initial load or silent poll) can resolve *after* the
		// copy's load — drop it instead of clobbering `server` with the source.
		const requestedId = id;
		if (loadingId === requestedId) return;
		loadingId = requestedId;
		const revision = mutationRevision;
		if (!silent) loadState = 'loading';
		try {
			const result = await getServer(requestedId);
			if (requestedId !== id) return; // route changed mid-flight; stale response
			if (revision !== mutationRevision) return; // an action returned newer state
			server = result;
			loadState = 'ready';
			loadError = null;
		} catch (err) {
			if (requestedId !== id) return;
			if (!silent) {
				loadState = 'error';
				loadError = errorMessage(err);
			}
		} finally {
			if (loadingId === requestedId) loadingId = null;
		}
	}

	const activeStartup = $derived(server ? hasActiveStartup(server) : false);
	const action = $derived(server ? primaryServerAction(server) : 'start');
	const startup = $derived(server?.startup_status ?? null);
	const startupElapsed = $derived(startup ? formatElapsed(startup.activation_started_at) : null);
	const startupCountdown = $derived(
		startup ? formatCountdown(startup.next_retry_at ?? startup.deadline_at) : null
	);
	const terminalFailure = $derived(
		!!server && !activeStartup && (server.state === 'failed' || server.state === 'unhealthy')
	);
	const priorityLogs = $derived(activeStartup || terminalFailure);

	async function runPrimaryAction() {
		if (!server || busy) return;
		busy = true;
		// Capture the id this action targets. Clone reuses this component (same-route
		// nav), so if the route changes mid-flight the resolved summary belongs to the
		// *previous* server — drop it instead of clobbering the copy with the source.
		const requestedId = id;
		try {
			const updated =
				action === 'stop'
					? await disableServer(requestedId)
					: action === 'retry'
						? await retryServer(requestedId)
						: await enableServer(requestedId);
			if (requestedId !== id || !server) return; // route changed mid-flight
			mutationRevision += 1;
			server = { ...server, ...updated };
		} catch (err) {
			if (requestedId === id) flashToast(errorMessage(err));
		} finally {
			busy = false;
		}
	}

	// Per-tool enable/disable (issue #105). The disabled set lives on the server row
	// (`disabled_tools`); the bridge hides those tools from every surface. A hidden tool
	// drops out of discovery, so it's absent from `server.tools` — the row list below
	// unions the discovered tools with the disabled names so a hidden tool stays visible
	// (and re-enableable) here.
	// Tool hide list, staged as a bulk edit. `base` is what's persisted on the server;
	// `pendingDisabled` holds the operator's unsaved switch flips (null when in sync). One
	// **Apply** sends a single PATCH → one bridge restart for the whole batch, which also
	// removes the concurrent-PATCH race a per-toggle apply had (only ever one write).
	const baseDisabled = $derived(new Set(server?.disabled_tools ?? []));
	let pendingDisabled = $state<Set<string> | null>(null);
	const effectiveDisabled = $derived(pendingDisabled ?? baseDisabled);
	let applyingTools = $state(false); // Apply PATCH + reload in flight

	function setsEqual(a: Set<string>, b: Set<string>): boolean {
		return a.size === b.size && [...a].every((x) => b.has(x));
	}

	const toolChangesDirty = $derived(
		pendingDisabled !== null && !setsEqual(pendingDisabled, baseDisabled)
	);

	// Drop staged edits when the viewed server changes (this component is reused across
	// same-route navigations — clone, sidebar). Guarded so it doesn't loop on its own write.
	let stagedForServerId: string | null = null;
	$effect(() => {
		const sid = server?.id ?? null;
		if (sid !== stagedForServerId) {
			stagedForServerId = sid;
			pendingDisabled = null;
		}
	});

	// One row per known tool: the discovered ones (in discovery order) plus any name that is
	// disabled (persisted OR staged) but no longer discovered (appended, sorted). `enabled`
	// reflects the STAGED state so the switch shows where the operator has set it.
	const toolRows = $derived.by(() => {
		if (!server) return [] as { tool: ServerTool; enabled: boolean }[];
		const seen = new Set<string>();
		const rows: { tool: ServerTool; enabled: boolean }[] = [];
		for (const tool of server.tools) {
			seen.add(tool.name);
			rows.push({ tool, enabled: !effectiveDisabled.has(tool.name) });
		}
		const undiscovered = new Set([...baseDisabled, ...(pendingDisabled ?? [])]);
		for (const name of [...undiscovered].sort()) {
			if (seen.has(name)) continue;
			// Disabled and no longer discovered: synthesize a minimal row so it can be re-enabled.
			rows.push({ tool: { name, description: '' }, enabled: !effectiveDisabled.has(name) });
		}
		return rows;
	});

	// Local-only switch flip: stage the change, don't touch the server until Apply. Skip
	// while an Apply or a lifecycle op (start/stop/delete/clone) is in flight.
	function toggleToolPending(name: string, enable: boolean) {
		if (!server || applyingTools || busy || deleting || cloning) return;
		const next = new Set(pendingDisabled ?? baseDisabled);
		if (enable) next.delete(name);
		else next.add(name);
		// Back in sync with the server → clear the staged set so background polls flow through.
		pendingDisabled = setsEqual(next, baseDisabled) ? null : next;
	}

	function revertToolChanges() {
		if (!applyingTools) pendingDisabled = null;
	}

	async function applyToolChanges() {
		if (!server || !toolChangesDirty || applyingTools || busy || deleting || cloning) return;
		const requestedId = id;
		const next = [...(pendingDisabled ?? baseDisabled)].sort();
		applyingTools = true;
		// Bump now so an in-flight background poll can't resolve and clobber the staged state.
		mutationRevision += 1;
		try {
			await updateServer(requestedId, { disabled_tools: next });
			if (requestedId !== id) return; // navigated away mid-flight
			pendingDisabled = null; // persisted — base now matches
			mutationRevision += 1;
			await load(true); // reconcile the discovered tool list after the restart
		} catch (err) {
			if (requestedId === id) flashToast(errorMessage(err));
		} finally {
			// Clear unconditionally (like busy/cloning): if the operator navigated to another
			// server mid-apply, the id guard would otherwise leave this true and freeze the
			// new view's switches + Apply. The route-change effect only resets pendingDisabled.
			applyingTools = false;
		}
	}

	async function doClone() {
		// Don't start a clone while an enable/disable, delete, or tool-apply is still in
		// flight — the toggle response is id-guarded above, but blocking here keeps the source
		// page from kicking off conflicting actions right before it navigates away (and a
		// navigate mid-apply is exactly what would strand the apply flag on the next view).
		if (!server || cloning || busy || deleting || applyingTools) return;
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
	let oauthGraceTimer: ReturnType<typeof setTimeout> | undefined;
	// Nonce of the flow THIS page started; broadcasts carrying any other nonce belong
	// to a different tab's sign-in and are ignored.
	let oauthNonce: string | null = null;

	// How long a COOP-severed flow (see watchOauthPopup) stays busy waiting for the
	// completion broadcast before giving up on an operator who abandoned the popup.
	const OAUTH_SEVERED_GRACE_MS = 90_000;

	function clearOauthTimers() {
		clearInterval(oauthPopupWatch);
		oauthPopupWatch = undefined;
		clearTimeout(oauthGraceTimer);
		oauthGraceTimer = undefined;
	}

	// While the popup is open, poll for the operator closing it mid-sign-in (there's no
	// event for that) so the Authorize button doesn't stay stuck busy. A completed flow
	// clears the watch via the broadcast listener before the popup closes itself.
	function watchOauthPopup(popup: Window) {
		clearOauthTimers();
		let polls = 0;
		oauthPopupWatch = setInterval(() => {
			polls += 1;
			if (!popup.closed) return;
			clearInterval(oauthPopupWatch);
			oauthPopupWatch = undefined;
			if (polls <= 2) {
				// `closed` flipped within ~a second of navigating to the provider: no
				// human dismisses a popup that fast — this is a provider serving its
				// auth pages with Cross-Origin-Opener-Policy, which severs our
				// WindowProxy (it reports closed=true while the popup is still open).
				// The flow is live, so STAY busy — re-enabling the button here would
				// invite a second click that cancels the in-flight grant server-side
				// (begin_authorization supersedes) — and let the completion broadcast
				// finish it. The grace timer catches an operator who abandons the
				// now-untrackable popup.
				oauthGraceTimer = setTimeout(() => {
					oauthGraceTimer = undefined;
					oauthBusy = false;
					void load(true);
				}, OAUTH_SEVERED_GRACE_MS);
				return;
			}
			// A real close (the operator dismissed the popup): re-enable the button
			// and refresh, but KEEP the nonce so a completion broadcast that raced
			// the close still lands.
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
			clearOauthTimers();
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
			clearOauthTimers();
		};
	});

	async function doDisconnect() {
		if (!server || oauthBusy) return;
		oauthBusy = true;
		try {
			const updated = await disconnectOauth(server.id);
			mutationRevision += 1;
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

	// Initial load and adaptive polling keep startup transitions current without
	// overlapping requests. Poll responses older than an action are dropped in load().
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
		clearOauthTimers();
		oauthBusy = false;
		void load();
	});

	let pollTick = $state(0);
	$effect(() => {
		void pollTick;
		void busy;
		void deleting;
		void oauthBusy;
		if (loadState !== 'ready' || !server || server.id !== id) return;
		const timer = setTimeout(async () => {
			if (!busy && !deleting && !oauthBusy) await load(true);
			pollTick += 1;
		}, pollingInterval([server]));
		return () => clearTimeout(timer);
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

	// The summary's resolved auth stays current through the existing server poll.
	const effectiveAuth = $derived(server?.auth ?? null);

	// Render the stored command + args as a single shell-ish line, quoting any
	// token that contains whitespace so the spacing reads correctly. For a docker
	// server the stored shape is (image, container args, run options) and the backend
	// synthesizes the real `docker run …` — mirror it honestly (hardening flags elided
	// as […], env passed by NAME only, run options before `--` + image).
	const commandLine = $derived.by(() => {
		const quote = (p: string) => (/\s/.test(p) ? `"${p}"` : p);
		if (server?.runner === 'docker') {
			return [
				'docker run -i --rm --init […]',
				...Object.keys(server.env ?? {}).flatMap((k) => ['-e', k]).map(quote),
				...(server.run_args ?? []).map(quote),
				'--',
				...[server.command, ...(server.args ?? [])].filter((p) => p.length > 0).map(quote)
			].join(' ');
		}
		return [server?.command ?? '', ...(server?.args ?? [])]
			.filter((p) => p.length > 0)
			.map(quote)
			.join(' ');
	});

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
					<StatePill state={server.state} startupStatus={startup} />
				</div>
				<p class="mt-1 truncate font-mono text-sm text-[var(--color-ink-dim)]">
					{server.slug}
				</p>
			</div>

			<div class="flex flex-wrap items-center justify-end gap-2">
				<button
					type="button"
					onclick={runPrimaryAction}
					disabled={busy}
					aria-busy={busy}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-semibold transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
					style={action === 'stop'
						? 'color: var(--color-ink-muted); border: 1px solid var(--color-line);'
						: 'color: var(--color-accent-ink); background-color: var(--color-accent);'}
				>
					{#if busy}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						{action === 'stop' ? 'Stopping' : action === 'retry' ? 'Retrying' : 'Starting'}
					{:else if action === 'stop'}
						<svg class="size-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
							<rect x="7" y="7" width="10" height="10" rx="1.5" />
						</svg>
						Stop
					{:else if action === 'retry'}
						<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
							<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />
						</svg>
						Retry
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
					disabled={cloning || busy || deleting || applyingTools}
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

		{#if startup}
			<div
				class="flex flex-col gap-2 rounded-[var(--radius-card)] border px-4 py-3"
				style="border-color: color-mix(in oklab, var(--color-state-starting) 35%, transparent); background-color: color-mix(in oklab, var(--color-state-starting) 8%, transparent);"
			>
				<div class="flex flex-wrap items-center justify-between gap-2">
					<p class="text-sm font-semibold text-[var(--color-ink)]">
						{startupPhaseLabel(startup.phase)}
					</p>
					<span class="font-mono text-xs text-[var(--color-ink-muted)]">
						Attempt {startup.attempt} of {startup.max_attempts}
					</span>
				</div>
				<div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--color-ink-dim)]">
					{#if startupElapsed}<span>{startupElapsed} elapsed</span>{/if}
					{#if startupCountdown}
						<span>
							{startup.next_retry_at
								? `Retry in ${startupCountdown}`
								: `${startupCountdown} until this phase times out`}
						</span>
					{/if}
				</div>
				{#if startup.message}
					<p class="font-mono text-xs leading-relaxed text-[var(--color-ink-muted)]">
						{startup.message}
					</p>
				{/if}
			</div>
		{/if}

		{#if !activeStartup && server.last_error}
			<p
				class="rounded-lg border px-3.5 py-3 font-mono text-xs leading-relaxed"
				style="border-color: color-mix(in oklab, var(--color-state-failed) 30%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 10%, transparent); color: var(--color-state-failed);"
			>
				{server.last_error}
			</p>
		{/if}

		{#if priorityLogs}
			<LogViewer
				serverId={server.id}
				serverState={server.state}
				startupStatus={startup}
				compact
			/>
		{/if}

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
				{#if server.transports.rest_openapi}
					<div class="flex items-center justify-between gap-3">
						<div class="min-w-0 flex-1">
							<p class="text-xs font-medium text-[var(--color-ink-muted)]">REST</p>
							<p class="truncate font-mono text-xs text-[var(--color-ink)]">
								{server.urls.rest ?? '— not exposed —'}
							</p>
							{#if server.urls.rest}
								<p class="mt-0.5 text-[11px] text-[var(--color-ink-dim)]">
									<code class="font-mono">POST {server.urls.rest}/&lt;tool&gt;</code> with the
									tool's JSON arguments · OpenAPI at
									<code class="font-mono">{server.urls.rest}/openapi.json</code>
								</p>
							{/if}
						</div>
						<CopyButton value={server.urls.rest} label="Copy" />
					</div>
				{/if}
			</div>
			{#if effectiveAuth === 'bearer'}
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
			{:else if effectiveAuth === 'oauth'}
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
						Requests need an access token from the OAuth authorization server configured in
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
				{#if !activeStartup && typeof server.pid === 'number'}
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">pid {server.pid}</span>
				{/if}
				{#if !activeStartup && typeof server.port === 'number'}
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">port {server.port}</span>
				{/if}
			</div>

			{#if server.setup_script}
				<div class="flex flex-col gap-1.5">
					<span class="text-xs font-medium text-[var(--color-ink-muted)]">Setup script</span>
					<pre class="m-0 overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5 font-mono text-xs whitespace-pre-wrap text-[var(--color-ink)]">{server.setup_script}</pre>
				</div>
			{/if}

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

		<!-- Runtime tool data can belong to the previous process while startup is active. -->
		{#if !activeStartup}
			<div
				class="flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
			>
				<div class="flex items-center justify-between">
					<h2 class="text-sm font-semibold text-[var(--color-ink)]">Tools</h2>
					<span class="font-mono text-xs text-[var(--color-ink-dim)]">{server.tools_count}</span>
				</div>
				{#if toolRows.length === 0}
					<p class="text-xs text-[var(--color-ink-dim)]">
						{server.state === 'running'
							? 'No tools discovered.'
							: 'Tools are discovered once the server is running.'}
					</p>
				{:else}
					<p class="text-xs text-[var(--color-ink-dim)]">
						Toggle tools off to hide them from clients (MCP, REST, and groups); hidden tools
						are also refused if called. Changes are staged — click <strong>Apply</strong> to
						save them in one restart.
					</p>
					<ul class="flex flex-col divide-y divide-[var(--color-line)]">
						{#each toolRows as { tool, enabled } (tool.name)}
							{@const changed = baseDisabled.has(tool.name) === enabled}
							<li class="flex items-start justify-between gap-3 py-2 first:pt-0 last:pb-0">
								<div class="flex min-w-0 flex-col gap-0.5" class:opacity-50={!enabled}>
									<span class="flex flex-wrap items-center gap-1.5">
										<span class="font-mono text-xs font-medium text-[var(--color-ink)]">
											{tool.name}
										</span>
										{#if !enabled}
											<span
												class="rounded-md border border-[var(--color-line)] px-1.5 py-0.5 text-[11px] font-medium text-[var(--color-ink-dim)]"
											>
												disabled
											</span>
										{/if}
										{#if changed}
											<span
												class="rounded-md border border-[var(--color-accent)] px-1.5 py-0.5 text-[11px] font-medium text-[var(--color-accent)]"
											>
												unsaved
											</span>
										{/if}
										{#if enabled && tool.has_output_schema === false}
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
									{#if enabled}
										<div class="mt-1">
											<ToolRunner
												serverId={server.id}
												{tool}
												runnable={server.state === 'running'}
											/>
										</div>
									{/if}
								</div>
								<button
									type="button"
									role="switch"
									aria-checked={enabled}
									aria-label={`${enabled ? 'Disable' : 'Enable'} ${tool.name}`}
									title={enabled ? 'Exposed — click to hide from clients' : 'Hidden — click to expose'}
									disabled={busy || deleting || cloning || applyingTools}
									onclick={() => toggleToolPending(tool.name, !enabled)}
									class="relative mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:cursor-not-allowed disabled:opacity-50 {enabled
										? 'bg-[var(--color-accent)]'
										: 'bg-[var(--color-line)]'}"
								>
									<span
										class="inline-block size-4 rounded-full bg-white shadow-sm transition {enabled
											? 'translate-x-[18px]'
											: 'translate-x-0.5'}"
									></span>
								</button>
							</li>
						{/each}
					</ul>
					{#if toolChangesDirty}
						<div
							class="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-card)] border border-[var(--color-accent)] bg-[var(--color-surface-2)] px-4 py-3"
						>
							<p class="text-xs text-[var(--color-ink-dim)]">
								Unsaved tool changes. Applying restarts the server once.
							</p>
							<div class="flex items-center gap-2">
								<button
									type="button"
									onclick={revertToolChanges}
									disabled={applyingTools}
									class="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink)] transition hover:bg-[var(--color-surface)] disabled:cursor-not-allowed disabled:opacity-50"
								>
									Revert
								</button>
								<button
									type="button"
									onclick={applyToolChanges}
									disabled={applyingTools || busy || deleting || cloning}
									class="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
								>
									{applyingTools ? 'Applying…' : 'Apply changes'}
								</button>
							</div>
						</div>
					{/if}
				{/if}
			</div>
		{/if}

		<!-- Logs -->
		{#if !priorityLogs}
			<LogViewer
				serverId={server.id}
				serverState={server.state}
				startupStatus={startup}
			/>
		{/if}

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
