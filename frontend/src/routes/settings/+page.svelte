<script lang="ts">
	import {
		createToken,
		deleteGroup,
		deleteToken,
		errorMessage,
		getAuthStatus,
		getSettings,
		listGroups,
		listServers,
		listTokens,
		putGroup,
		updateSettings
	} from '$lib/api';
	import type {
		AuthProvider,
		BindMode,
		ControlPlaneAuth,
		GroupInfo,
		GroupMembers,
		ServerSummary,
		SettingsInfo,
		TokenCreated,
		TokenInfo
	} from '$lib/types';
	import { clearToken, setToken } from '$lib/auth';
	import { isLoopbackHost, isPrivateIpHost, normalizeHost } from '$lib/host';
	import CopyButton from '$lib/components/CopyButton.svelte';
	import CopyMenu from '$lib/components/CopyMenu.svelte';
	import Toast from '$lib/components/Toast.svelte';

	type LoadState = 'loading' | 'ready' | 'error';

	// ---- Toast ----------------------------------------------------------------
	let toast = $state<{ message: string; tone: 'error' | 'info' } | null>(null);
	let toastTimer: ReturnType<typeof setTimeout> | undefined;
	function flashToast(message: string, tone: 'error' | 'info' = 'error') {
		toast = { message, tone };
		clearTimeout(toastTimer);
		toastTimer = setTimeout(() => (toast = null), 6000);
	}

	// ---- Load -----------------------------------------------------------------
	let loadState = $state<LoadState>('loading');
	let loadError = $state<string | null>(null);

	let settings = $state<SettingsInfo | null>(null);
	let tokens = $state<TokenInfo[]>([]);
	let servers = $state<ServerSummary[]>([]);
	let groups = $state<GroupInfo[]>([]);
	// Whether THIS browser holds a working control token (not just whether one exists
	// in the backend). That's what decides if enabling enforcement would lock us out.
	let hasUsableAdminCredential = $state(false);

	async function load() {
		loadState = 'loading';
		loadError = null;
		try {
			const [s, t, srv, grp, auth] = await Promise.all([
				getSettings(),
				listTokens(),
				listServers(),
				listGroups(),
				getAuthStatus()
			]);
			settings = s;
			tokens = t;
			servers = srv;
			groups = grp;
			hasUsableAdminCredential = auth.authenticated;
			loadState = 'ready';
		} catch (err) {
			loadState = 'error';
			loadError = errorMessage(err);
		}
	}

	$effect(() => {
		load();
		return () => clearTimeout(toastTimer);
	});

	// ---- Access tokens --------------------------------------------------------

	// New-token flow: name entry → scope choice → POST → one-time plaintext reveal.
	let newTokenName = $state('');
	let newTokenScope = $state('all');
	let creating = $state(false);
	let createdToken = $state<TokenCreated | null>(null);

	const newNameValid = $derived(newTokenName.trim().length > 0);

	async function handleCreateToken(e: SubmitEvent) {
		e.preventDefault();
		if (creating || !newNameValid) return;
		creating = true;
		try {
			const created = await createToken(newTokenName.trim(), newTokenScope);
			createdToken = created;
			// List it (by prefix) immediately; the reveal box holds the plaintext.
			const { token: _token, ...info } = created;
			void _token;
			tokens = [info, ...tokens];
			newTokenName = '';
			newTokenScope = 'all';
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			creating = false;
		}
	}

	/** A server's display label. Names aren't unique (the backend only makes slugs
	 * unique), so append the slug to disambiguate when another server shares the
	 * name — otherwise a scope choice could point at the wrong server. */
	function serverLabel(server: ServerSummary): string {
		const collides = servers.some((s) => s.id !== server.id && s.name === server.name);
		return collides ? `${server.name} (${server.slug})` : server.name;
	}

	/** Human label for a token's scope: 'All servers', a group, the server's label, or
	 * a fallback when the scoped server/group no longer exists. */
	function scopeLabel(scope: string): string {
		if (scope === 'all') return 'All servers';
		if (scope.startsWith('group:')) {
			const name = scope.slice('group:'.length);
			return groups.some((g) => g.name === name) ? `Group: ${name}` : 'Unknown group';
		}
		const server = servers.find((s) => s.id === scope);
		return server ? serverLabel(server) : 'Unknown server';
	}

	function dismissReveal() {
		createdToken = null;
	}

	// Mint a control-scope admin token, reveal it once, and log in immediately so
	// the operator stays signed in after turning enforcement on.
	let generatingAdmin = $state(false);
	async function handleGenerateAdmin() {
		if (generatingAdmin) return;
		generatingAdmin = true;
		try {
			const created = await createToken('admin', 'control');
			createdToken = created;
			const { token: _token, ...info } = created;
			void _token;
			tokens = [info, ...tokens];
			setToken(created.token);
			hasUsableAdminCredential = true; // we now hold a working control token
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			generatingAdmin = false;
		}
	}

	// Revoke flow: a per-row confirm gate, then DELETE.
	let confirmRevokeId = $state<string | null>(null);
	let revokingId = $state<string | null>(null);

	async function handleRevoke(id: string) {
		if (revokingId) return;
		revokingId = id;
		try {
			await deleteToken(id);
			tokens = tokens.filter((t) => t.id !== id);
			// If the revealed token was the one revoked, drop the reveal too.
			if (createdToken?.id === id) createdToken = null;
			// Revoking may have invalidated this browser's stored admin token, so re-check
			// rather than leaving expose/always enabled on a now-dead credential.
			const auth = await getAuthStatus();
			hasUsableAdminCredential = auth.authenticated;
			if (!auth.authenticated) clearToken();
			flashToast('Token revoked', 'info');
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			revokingId = null;
			confirmRevokeId = null;
		}
	}

	function formatDate(iso: string): string {
		const d = new Date(iso);
		if (Number.isNaN(d.getTime())) return iso;
		return d.toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
	}

	// ---- Security settings ----------------------------------------------------

	const AUTH_CHOICES: { value: AuthProvider; label: string }[] = [
		{ value: 'none', label: 'none' },
		{ value: 'bearer', label: 'bearer' }
	];
	const BIND_CHOICES: { value: BindMode; label: string; hint: string }[] = [
		{ value: 'local', label: 'local', hint: 'Loopback only' },
		{ value: 'expose', label: 'expose', hint: 'Reachable off-host' }
	];
	const CONTROL_AUTH_CHOICES: { value: ControlPlaneAuth; label: string; hint: string }[] = [
		{ value: 'auto', label: 'auto', hint: 'Required when exposed' },
		{ value: 'always', label: 'always', hint: 'Required even on loopback' }
	];

	// Persist a settings patch (save-on-change), optimistically applying it and
	// rolling back on failure so the controls never drift from the backend.
	let savingField = $state<keyof SettingsInfo | null>(null);

	async function patchSettings(patch: Partial<SettingsInfo>, field: keyof SettingsInfo) {
		// Serialize saves: PATCH returns the full settings object, so an overlapping
		// save could clobber a newer field. Allow one in-flight save at a time.
		if (!settings || savingField) return;
		const previous = settings;
		settings = { ...settings, ...patch };
		savingField = field;
		try {
			settings = await updateSettings(patch);
		} catch (err) {
			settings = previous; // roll back
			flashToast(errorMessage(err));
		} finally {
			savingField = null;
		}
	}

	// ---- Self-lockout guard rails ---------------------------------------------
	// The control plane enforces a Host/Origin allowlist on EVERY request: loopback
	// (localhost/127.0.0.1/::1) always passes; a non-loopback host passes only in
	// `expose` mode while it's in `allowed_hosts`. So if this tab was reached via a
	// non-loopback host, two actions would 403 our own /api calls and brick the UI:
	// removing that host from the allowlist, or switching to `local` (which ignores
	// the allowlist entirely). Loopback always recovers, so we confirm — not block.
	// Browser-only SPA (ssr/prerender disabled), so `window` is defined in practice.
	// The `typeof window` guards keep this module import-safe in a non-browser context
	// too (a node test runner, or if SSR is ever turned on). When absent, browserHost
	// is empty so `onLoopback` is false — the fail-safe direction (guards armed).
	const browserHost =
		typeof window !== 'undefined' ? normalizeHost(window.location.hostname) : '';
	const onLoopback = isLoopbackHost(browserHost);
	// Reached through a private-IP literal (e.g. 192.168.1.50) but not loopback: this
	// tab relies on `allow_private_lan`, so turning it off would 403 our own /api.
	const onPrivateLan = !onLoopback && isPrivateIpHost(browserHost);
	const loopbackUrl =
		typeof window !== 'undefined'
			? `${window.location.protocol}//localhost${window.location.port ? `:${window.location.port}` : ''}`
			: '';

	let confirmBindLocal = $state(false);
	let confirmRemoveHost = $state<string | null>(null);
	let confirmDisableLan = $state(false);

	// The bind-mode radios are controlled by `settings.bind_mode`. When we intercept a
	// selection to confirm (without patching), Svelte won't re-assert the radios'
	// `checked` — the value it would write is unchanged — so the just-selected option
	// stays visually checked. Snap them back to the real value imperatively (covers
	// mouse and keyboard, both of which fire the change handler).
	function resetBindRadios() {
		if (!settings) return;
		for (const el of document.querySelectorAll<HTMLInputElement>('input[name="bind-mode"]')) {
			el.checked = el.value === settings.bind_mode;
		}
	}

	function setDefaultAuth(value: AuthProvider) {
		if (!settings || settings.default_auth_provider === value) return;
		patchSettings({ default_auth_provider: value }, 'default_auth_provider');
	}

	function setBindMode(value: BindMode) {
		if (!settings || settings.bind_mode === value) return;
		if (value === 'expose' && !hasUsableAdminCredential) {
			flashToast('Generate an admin token before exposing the control plane.');
			resetBindRadios();
			return;
		}
		// Switching to `local` from a non-loopback host locks this tab out (local
		// allows loopback only). Hold the change behind an explicit confirm.
		if (value === 'local' && !onLoopback) {
			confirmBindLocal = true;
			resetBindRadios(); // keep `expose` shown while the confirm is open
			return;
		}
		patchSettings({ bind_mode: value }, 'bind_mode');
	}

	function setControlPlaneAuth(value: ControlPlaneAuth) {
		if (!settings || settings.control_plane_auth === value) return;
		if (value === 'always' && !hasUsableAdminCredential) {
			flashToast('Generate an admin token before requiring control-plane auth.');
			return;
		}
		patchSettings({ control_plane_auth: value }, 'control_plane_auth');
	}

	function confirmSwitchToLocal() {
		confirmBindLocal = false;
		patchSettings({ bind_mode: 'local' }, 'bind_mode');
	}

	function cancelSwitchToLocal() {
		confirmBindLocal = false;
		resetBindRadios();
	}

	// allow_private_lan: opening to LAN devices makes the box reachable off-host, so
	// (under `auto`) it turns control-plane auth on — hence the admin-token gate when
	// enabling, mirroring `expose`. Turning it OFF while this tab is reached through a
	// private IP would lock the tab out, so confirm that direction.
	// A checkbox flips its own `checked` on click; when we intercept without patching,
	// Svelte won't re-assert it (the bound state is unchanged), so snap `el` back to the
	// real value imperatively — same idea as resetBindRadios() for the radios.
	function setAllowPrivateLan(value: boolean, el: HTMLInputElement) {
		if (!settings || settings.allow_private_lan === value) return;
		if (value && !hasUsableAdminCredential) {
			flashToast('Generate an admin token before opening the control plane to the LAN.');
			el.checked = settings.allow_private_lan;
			return;
		}
		if (!value && onPrivateLan) {
			confirmDisableLan = true;
			el.checked = settings.allow_private_lan; // keep it shown on while the confirm is open
			return;
		}
		patchSettings({ allow_private_lan: value }, 'allow_private_lan');
	}

	function confirmDisablePrivateLan() {
		confirmDisableLan = false;
		patchSettings({ allow_private_lan: false }, 'allow_private_lan');
	}

	// docker_runner: root-equivalent — enabling it lets servers run arbitrary Docker
	// images on the mounted daemon. Confirm before turning ON; turning off is always safe.
	let confirmEnableDocker = $state(false);

	function setDockerRunner(value: boolean, el: HTMLInputElement) {
		if (!settings || settings.docker_runner === value) return;
		if (value) {
			confirmEnableDocker = true;
			el.checked = settings.docker_runner; // keep it off until confirmed
			return;
		}
		patchSettings({ docker_runner: false }, 'docker_runner');
	}

	function confirmEnableDockerRunner() {
		confirmEnableDocker = false;
		patchSettings({ docker_runner: true }, 'docker_runner');
	}

	// ---- Groups (the /g/<name> registry) ----------------------------------------
	// A group is a named bundle served at /g/<name>/mcp — the union of its running
	// members' tools, namespaced by slug. Membership is "*" (every registered server,
	// present and future) or an explicit list of server ids. There is no special-case
	// name; add a group named "all" with members "*" for a bundle of everything.
	let newGroupName = $state('');
	let newGroupMode = $state<'all' | 'selected'>('all');
	let newGroupSelection = $state<string[]>([]);
	let savingGroup = $state(false);
	let deletingGroup = $state<string | null>(null);

	// A group name is the URL routing key, so mirror the backend grammar (lowercase
	// alphanumerics + single hyphens) — reject anything that couldn't be routed.
	const groupNameValid = $derived(/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(newGroupName.trim()));

	/** Human label for a group's membership: "all servers" for the wildcard, else a
	 * count. */
	function membersLabel(members: GroupMembers): string {
		if (members === '*') return 'all servers';
		return members.length === 1 ? '1 server' : `${members.length} servers`;
	}

	/** Synthetic summary so the existing CopyMenu (client-config snippets) works for a
	 * group's /g/<name>/mcp URL without any changes to install.ts. */
	function groupSummary(group: GroupInfo): ServerSummary {
		return {
			id: `group:${group.name}`,
			slug: group.name,
			name: `Group: ${group.name}`,
			runner: 'remote',
			enabled: true,
			state: 'running',
			transports: { mcp_http: true, rest_openapi: false },
			urls: { mcp: group.url, rest: null },
			auth: settings?.default_auth_provider === 'bearer' ? 'bearer' : 'none',
			last_error: null,
			pid: null,
			port: null,
			tools_count: 0
		} satisfies ServerSummary;
	}

	function toggleNewGroupServer(id: string, included: boolean) {
		newGroupSelection = included
			? [...newGroupSelection, id]
			: newGroupSelection.filter((x) => x !== id);
	}

	async function refreshGroups() {
		try {
			groups = await listGroups();
		} catch (err) {
			flashToast(errorMessage(err));
		}
	}

	// A pending replace: set when the entered name matches an existing group, so the
	// create form doubles as an in-place editor (putGroup replaces membership) WITHOUT
	// silently overwriting — the operator confirms first. Cleared automatically once the
	// name no longer matches (the banner's condition re-checks it).
	let confirmReplaceName = $state<string | null>(null);

	async function submitGroup() {
		const name = newGroupName.trim();
		const members: GroupMembers = newGroupMode === 'all' ? '*' : newGroupSelection;
		savingGroup = true;
		try {
			await putGroup(name, members);
			await refreshGroups();
			newGroupName = '';
			newGroupMode = 'all';
			newGroupSelection = [];
			confirmReplaceName = null;
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			savingGroup = false;
		}
	}

	function handleCreateGroup(e: SubmitEvent) {
		e.preventDefault();
		if (savingGroup || !groupNameValid) return;
		const name = newGroupName.trim();
		// putGroup has replace semantics. If the name is taken, require an explicit
		// confirm so membership is never *silently* overwritten — but still allow editing
		// an existing group in place, rather than forcing a delete+recreate that would
		// take /g/<name>/mcp offline and revoke its group:<name> tokens.
		if (groups.some((g) => g.name === name)) {
			confirmReplaceName = name;
			return;
		}
		void submitGroup();
	}

	// Delete needs an explicit confirm (it takes a live /g/<name>/mcp endpoint offline),
	// mirroring the per-row token-revoke gate.
	let confirmDeleteGroup = $state<string | null>(null);

	async function handleDeleteGroup(name: string) {
		if (deletingGroup) return;
		deletingGroup = name;
		try {
			await deleteGroup(name);
			groups = groups.filter((g) => g.name !== name);
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			deletingGroup = null;
			confirmDeleteGroup = null;
		}
	}

	// ---- Allowed hosts editor -------------------------------------------------
	let newHost = $state('');

	function addHost(e: SubmitEvent) {
		e.preventDefault();
		if (!settings) return;
		let host = newHost.trim();
		if (!host) return;
		// If a full URL is pasted, keep just the hostname — the backend compares
		// hostnames only, so a scheme/port would never match the allowlist.
		if (host.includes('://')) {
			try {
				host = new URL(host).hostname;
			} catch {
				// not a parseable URL — fall through with the raw input
			}
		}
		if (settings.allowed_hosts.includes(host)) {
			newHost = '';
			flashToast(`${host} is already allowed`, 'info');
			return;
		}
		const next = [...settings.allowed_hosts, host];
		newHost = '';
		patchSettings({ allowed_hosts: next }, 'allowed_hosts');
	}

	function performRemoveHost(host: string) {
		if (!settings) return;
		const next = settings.allowed_hosts.filter((h) => h !== host);
		patchSettings({ allowed_hosts: next }, 'allowed_hosts');
	}

	function removeHost(host: string) {
		if (!settings) return;
		// Removing the host this tab is reached through locks the UI out. Confirm
		// first; any other host removes immediately.
		if (!onLoopback && normalizeHost(host) === browserHost) {
			confirmRemoveHost = host;
			return;
		}
		performRemoveHost(host);
	}

	function confirmRemoveCurrentHost() {
		const host = confirmRemoveHost;
		confirmRemoveHost = null;
		if (host !== null) performRemoveHost(host);
	}
</script>

<svelte:head>
	<title>Settings — mcpelevator</title>
</svelte:head>

<section class="mx-auto flex w-full max-w-2xl flex-col gap-8">
	<!-- Heading -->
	<div>
		<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">Settings</h1>
		<p class="mt-1 text-sm text-[var(--color-ink-muted)]">
			Access tokens and network security.
		</p>
	</div>

	{#if loadState === 'loading'}
		<div class="flex flex-col gap-4">
			{#each Array(2) as _, i (i)}
				<div
					class="h-40 animate-pulse rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)]"
				></div>
			{/each}
		</div>
	{:else if loadState === 'error'}
		<div
			class="flex flex-col items-center justify-center gap-4 rounded-[var(--radius-card)] border border-dashed border-[var(--color-line-strong)] px-6 py-16 text-center"
		>
			<p class="text-base font-semibold text-[var(--color-ink)]">Couldn't load settings</p>
			<p class="mx-auto max-w-sm text-sm text-[var(--color-ink-muted)]">{loadError}</p>
			<button
				type="button"
				onclick={load}
				class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink)] transition active:translate-y-px hover:border-[var(--color-line-strong)]"
			>
				Retry
			</button>
		</div>
	{:else if settings}
		<!-- ============================ Access tokens ============================ -->
		<div
			class="flex flex-col gap-5 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<div class="flex flex-col gap-1">
				<h2 class="text-sm font-semibold text-[var(--color-ink)]">Access tokens</h2>
				<p class="text-xs text-[var(--color-ink-dim)]">
					Bearer tokens for servers set to <code class="font-mono">bearer</code> auth.
					Sent as <code class="font-mono">Authorization: Bearer &lt;token&gt;</code>.
				</p>
			</div>

			<!-- One-time plaintext reveal -->
			{#if createdToken}
				<div
					class="flex flex-col gap-3 rounded-lg border p-4"
					style="border-color: color-mix(in oklab, var(--color-accent) 45%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);"
				>
					<div class="flex items-start gap-2.5">
						<svg
							class="mt-0.5 size-4 shrink-0 text-[var(--color-accent)]"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="M12 9v4M12 17h.01" />
							<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
						</svg>
						<div class="flex flex-col gap-0.5">
							<p class="text-sm font-semibold text-[var(--color-ink)]">
								Token created — <span class="font-mono">{createdToken.name}</span>
							</p>
							<p class="text-xs text-[var(--color-ink-muted)]">
								Copy this now — it won't be shown again.
							</p>
						</div>
					</div>
					<div class="flex items-center gap-2">
						<code
							class="min-w-0 flex-1 overflow-x-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-base)] px-3 py-2.5 font-mono text-xs whitespace-nowrap text-[var(--color-accent)]"
						>
							{createdToken.token}
						</code>
						<CopyButton value={createdToken.token} label="Copy" />
					</div>
					<div class="flex justify-end">
						<button
							type="button"
							onclick={dismissReveal}
							class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
						>
							I've copied it — dismiss
						</button>
					</div>
				</div>
			{/if}

			<!-- New token -->
			<form onsubmit={handleCreateToken} class="flex flex-wrap items-end gap-2">
				<div class="flex min-w-0 flex-1 basis-48 flex-col gap-1.5">
					<label for="token-name" class="text-xs font-medium text-[var(--color-ink-muted)]">
						New token
					</label>
					<input
						id="token-name"
						type="text"
						bind:value={newTokenName}
						autocomplete="off"
						spellcheck="false"
						placeholder="e.g. claude-desktop"
						class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
					/>
				</div>
				<div class="flex min-w-0 flex-col gap-1.5">
					<label for="token-scope" class="text-xs font-medium text-[var(--color-ink-muted)]">
						Scope
					</label>
					<select
						id="token-scope"
						bind:value={newTokenScope}
						class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm text-[var(--color-ink)] outline-none transition focus:border-[var(--color-line-strong)]"
					>
						<option value="all">All servers</option>
						{#if groups.length > 0}
							<optgroup label="Groups">
								{#each groups as group (group.name)}
									<option value={`group:${group.name}`}>Group: {group.name}</option>
								{/each}
							</optgroup>
						{/if}
						<optgroup label="Servers">
							{#each servers as server (server.id)}
								<option value={server.id}>{serverLabel(server)}</option>
							{/each}
						</optgroup>
					</select>
				</div>
				<button
					type="submit"
					disabled={!newNameValid || creating}
					aria-busy={creating}
					class="inline-flex shrink-0 items-center gap-2 rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)] disabled:cursor-not-allowed disabled:opacity-50"
				>
					{#if creating}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
					{/if}
					Create
				</button>
			</form>

			<!-- Admin (control-plane) token: the credential the SPA logs in with. -->
			<div
				class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-dashed border-[var(--color-line)] px-3 py-2.5"
			>
				<p class="min-w-0 text-xs text-[var(--color-ink-muted)]">
					<span class="font-medium text-[var(--color-ink)]">Admin token.</span> The credential
					you log in with when the control plane enforces auth.
				</p>
				<button
					type="button"
					onclick={handleGenerateAdmin}
					disabled={generatingAdmin}
					aria-busy={generatingAdmin}
					class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink)] transition hover:border-[var(--color-line-strong)] disabled:opacity-50"
				>
					Generate admin token
				</button>
			</div>

			<!-- Token list -->
			{#if tokens.length === 0}
				<p
					class="rounded-lg border border-dashed border-[var(--color-line)] px-3 py-6 text-center text-xs text-[var(--color-ink-dim)]"
				>
					No tokens yet. Create one above to authenticate bearer-protected servers.
				</p>
			{:else}
				<ul class="flex flex-col divide-y divide-[var(--color-line)]">
					{#each tokens as token (token.id)}
						<li class="flex items-center justify-between gap-3 py-3 first:pt-0 last:pb-0">
							<div class="flex min-w-0 flex-col gap-1">
								<span class="flex min-w-0 items-center gap-2">
									<span class="truncate text-sm font-medium text-[var(--color-ink)]">
										{token.name}
									</span>
									{#if token.scope === 'control'}
										<span
											class="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium whitespace-nowrap"
											title="Control-plane admin token, authenticates /api"
											style="border: 1px solid color-mix(in oklab, var(--color-accent) 40%, transparent); color: var(--color-accent);"
										>
											control
										</span>
									{:else}
										<span
											class="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium whitespace-nowrap"
											title={token.scope === 'all'
												? 'Authorizes every bearer-protected server and group'
												: token.scope.startsWith('group:')
													? 'Authorizes only this group'
													: 'Authorizes only this server'}
											style={token.scope === 'all'
												? 'border: 1px solid var(--color-line); color: var(--color-ink-muted);'
												: 'border: 1px solid color-mix(in oklab, var(--color-accent) 40%, transparent); color: var(--color-accent);'}
										>
											{scopeLabel(token.scope)}
										</span>
									{/if}
								</span>
								<span class="font-mono text-xs text-[var(--color-ink-dim)]">
									{token.prefix}…
									<span class="text-[var(--color-ink-dim)]">· {formatDate(token.created_at)}</span>
								</span>
							</div>
							{#if confirmRevokeId === token.id}
								<div class="flex shrink-0 items-center gap-1.5">
									<button
										type="button"
										onclick={() => handleRevoke(token.id)}
										disabled={revokingId === token.id}
										aria-busy={revokingId === token.id}
										class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
										style="background-color: var(--color-state-failed);"
									>
										{#if revokingId === token.id}
											<svg class="size-3.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
												<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
												<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
											</svg>
										{/if}
										Revoke
									</button>
									<button
										type="button"
										onclick={() => (confirmRevokeId = null)}
										disabled={revokingId === token.id}
										class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-50"
									>
										Cancel
									</button>
								</div>
							{:else}
								<button
									type="button"
									onclick={() => (confirmRevokeId = token.id)}
									class="shrink-0 rounded-lg border px-3 py-1.5 text-xs font-medium transition active:translate-y-px"
									style="border-color: color-mix(in oklab, var(--color-state-failed) 35%, transparent); color: var(--color-state-failed);"
								>
									Revoke
								</button>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</div>

		<!-- ============================== Security =============================== -->
		<div
			class="flex flex-col gap-6 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5"
		>
			<h2 class="text-sm font-semibold text-[var(--color-ink)]">Security</h2>

			<!-- Default auth -->
			<fieldset class="flex flex-col gap-2 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">Default auth</legend>
				<div
					class="grid grid-cols-2 gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-1"
				>
					{#each AUTH_CHOICES as choice (choice.value)}
						<label
							class="flex cursor-pointer items-center justify-center rounded-md px-3 py-1.5 font-mono text-xs font-semibold transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
							style={settings.default_auth_provider === choice.value
								? 'background-color: color-mix(in oklab, var(--color-accent) 14%, transparent); color: var(--color-accent);'
								: 'color: var(--color-ink-muted);'}
						>
							<input
								type="radio"
								name="default-auth"
								value={choice.value}
								checked={settings.default_auth_provider === choice.value}
								onchange={() => setDefaultAuth(choice.value)}
								disabled={savingField === 'default_auth_provider'}
								class="sr-only"
							/>
							{choice.label}
						</label>
					{/each}
				</div>
				<p class="text-xs text-[var(--color-ink-dim)]">
					Servers set to <code class="font-mono">inherit</code> use this.
					<code class="font-mono">bearer</code> requires a token in
					<code class="font-mono">Authorization: Bearer …</code>.
				</p>
			</fieldset>

			<!-- Bind mode -->
			<fieldset class="flex flex-col gap-2 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">Bind mode</legend>
				<div class="grid grid-cols-2 gap-2">
					{#each BIND_CHOICES as choice (choice.value)}
						{@const locked = choice.value === 'expose' && !hasUsableAdminCredential}
						<label
							class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
							class:cursor-not-allowed={locked}
							class:opacity-60={locked}
							style={settings.bind_mode === choice.value
								? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
								: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
						>
							<span class="flex items-center gap-2">
								<input
									type="radio"
									name="bind-mode"
									value={choice.value}
									checked={settings.bind_mode === choice.value}
									onchange={() => setBindMode(choice.value)}
									disabled={savingField === 'bind_mode'}
									class="sr-only"
								/>
								<span
									class="font-mono text-sm font-semibold"
									style={settings.bind_mode === choice.value
										? 'color: var(--color-accent);'
										: 'color: var(--color-ink);'}
								>
									{choice.label}
								</span>
							</span>
							<span class="text-[11px] leading-tight text-[var(--color-ink-dim)]">
								{choice.hint}
							</span>
						</label>
					{/each}
				</div>
				{#if confirmBindLocal}
					<div
						role="alert"
						class="flex flex-col gap-2.5 rounded-lg border p-3"
						style="border-color: color-mix(in oklab, var(--color-state-starting) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-starting) 8%, transparent);"
					>
						<p class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
							This will lock this browser
							(<code class="font-mono text-[var(--color-ink)]">{browserHost}</code>)
							out of the control plane — <code class="font-mono">local</code> allows loopback
							only. Open
							<a
								class="font-mono text-[var(--color-accent)] underline"
								href={loopbackUrl}
								target="_blank"
								rel="noopener noreferrer">{loopbackUrl}</a
							>
							first, then switch.
						</p>
						<div class="flex items-center gap-1.5">
							<button
								type="button"
								onclick={confirmSwitchToLocal}
								disabled={savingField !== null}
								class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
								style="background-color: var(--color-state-failed);"
							>
								Switch to local anyway
							</button>
							<button
								type="button"
								onclick={cancelSwitchToLocal}
								class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
							>
								Cancel
							</button>
						</div>
					</div>
				{/if}
				<p class="text-xs text-[var(--color-ink-dim)]">
					Host/Origin is always checked (DNS-rebinding defense): loopback is always
					allowed; <code class="font-mono">expose</code> also allows the hosts below.
				</p>
			</fieldset>

			<!-- Local network (LAN) access -->
			<fieldset class="flex flex-col gap-2 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">Local network access</legend>
				<label
					class="flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
					class:cursor-not-allowed={!settings.allow_private_lan && !hasUsableAdminCredential}
					class:opacity-60={!settings.allow_private_lan && !hasUsableAdminCredential}
					style={settings.allow_private_lan
						? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
						: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
				>
					<input
						type="checkbox"
						checked={settings.allow_private_lan}
						onchange={(e) => setAllowPrivateLan(e.currentTarget.checked, e.currentTarget)}
						disabled={savingField === 'allow_private_lan'}
						class="mt-0.5 size-4 shrink-0 accent-[var(--color-accent)]"
					/>
					<span class="flex flex-col gap-0.5">
						<span class="text-sm font-medium text-[var(--color-ink)]">
							Allow access from devices on your local network
						</span>
						<span class="text-[11px] leading-tight text-[var(--color-ink-dim)]">
							Reach this box at its private IP (e.g.
							<code class="font-mono">http://192.168.1.50:8080</code>) from other LAN devices —
							no per-host allowlisting. Private-IP literals only, from a private-network peer,
							so it stays DNS-rebinding-safe. Counts as exposed, so an admin token is required
							for <code class="font-mono">/api</code>.
						</span>
					</span>
				</label>
				{#if confirmDisableLan}
					<div
						role="alert"
						class="flex flex-col gap-2.5 rounded-lg border p-3"
						style="border-color: color-mix(in oklab, var(--color-state-starting) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-starting) 8%, transparent);"
					>
						<p class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
							This will lock this browser
							(<code class="font-mono text-[var(--color-ink)]">{browserHost}</code>)
							out of the control plane — you're connected through a private IP. Open
							<a
								class="font-mono text-[var(--color-accent)] underline"
								href={loopbackUrl}
								target="_blank"
								rel="noopener noreferrer">{loopbackUrl}</a
							>
							first, then turn it off.
						</p>
						<div class="flex items-center gap-1.5">
							<button
								type="button"
								onclick={confirmDisablePrivateLan}
								disabled={savingField !== null}
								class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
								style="background-color: var(--color-state-failed);"
							>
								Turn off anyway
							</button>
							<button
								type="button"
								onclick={() => (confirmDisableLan = false)}
								class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
							>
								Cancel
							</button>
						</div>
					</div>
				{/if}
			</fieldset>

			<!-- Docker runner (root-equivalent, opt-in) -->
			<fieldset class="flex flex-col gap-2 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">Docker runner</legend>
				<label
					class="flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
					style={settings.docker_runner
						? 'border-color: color-mix(in oklab, var(--color-state-failed) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 7%, transparent);'
						: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
				>
					<input
						type="checkbox"
						checked={settings.docker_runner}
						onchange={(e) => setDockerRunner(e.currentTarget.checked, e.currentTarget)}
						disabled={savingField === 'docker_runner'}
						class="mt-0.5 size-4 shrink-0 accent-[var(--color-accent)]"
					/>
					<span class="flex flex-col gap-0.5">
						<span class="text-sm font-medium text-[var(--color-ink)]">
							Enable running MCP servers packaged as Docker images
						</span>
						<span class="text-[11px] leading-tight text-[var(--color-ink-dim)]">
							<span class="font-semibold text-[var(--color-state-failed)]">Root-equivalent.</span>
							Launches arbitrary images on the mounted Docker daemon (a sibling container on
							the host, or an isolated dind sidecar). Only enable it if you trust every image
							you run and understand it can affect the host. Requires the Docker socket to be
							mounted into this container.
						</span>
					</span>
				</label>
				{#if confirmEnableDocker}
					<div
						role="alert"
						class="flex flex-col gap-2.5 rounded-lg border p-3"
						style="border-color: color-mix(in oklab, var(--color-state-failed) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 8%, transparent);"
					>
						<p class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
							The docker runner is <span class="font-semibold text-[var(--color-ink)]">root-equivalent</span>:
							a server you run can execute any image on the host's Docker daemon. Enable it only
							if you trust the images you'll run.
						</p>
						<div class="flex items-center gap-1.5">
							<button
								type="button"
								onclick={confirmEnableDockerRunner}
								disabled={savingField !== null}
								class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
								style="background-color: var(--color-state-failed);"
							>
								Enable anyway
							</button>
							<button
								type="button"
								onclick={() => (confirmEnableDocker = false)}
								class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
							>
								Cancel
							</button>
						</div>
					</div>
				{/if}
			</fieldset>

			<!-- Groups -->
			<fieldset class="flex flex-col gap-3 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">Groups</legend>
				<p class="text-xs text-[var(--color-ink-dim)]">
					A group is served at <code class="font-mono">/g/&lt;name&gt;/mcp</code> — one URL
					bundling its running members' tools, each prefixed by the member's slug (e.g.
					<code class="font-mono">github_create_issue</code>). Members are
					<code class="font-mono">all servers</code> (every registered server, including
					future ones) or a picked list. A group uses the default auth provider above; when
					that is <code class="font-mono">none</code>, members with stricter (bearer) auth are
					excluded so they can't be reached auth-free. Name a group
					<code class="font-mono">all</code> with <code class="font-mono">all servers</code>
					for a bundle of everything.
				</p>

				<!-- Existing groups -->
				{#if groups.length === 0}
					<p
						class="rounded-lg border border-dashed border-[var(--color-line)] px-3 py-4 text-center text-xs text-[var(--color-ink-dim)]"
					>
						No groups yet. Create one below to serve a bundle at
						<code class="font-mono">/g/&lt;name&gt;/mcp</code>.
					</p>
				{:else}
					<ul class="flex flex-col gap-2">
						{#each groups as group (group.name)}
							<li
								class="flex flex-col gap-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2.5"
							>
								<div class="flex items-center justify-between gap-2">
									<div class="flex min-w-0 flex-col gap-0.5">
										<span class="truncate font-mono text-sm font-semibold text-[var(--color-ink)]">
											{group.name}
										</span>
										<span class="text-[11px] text-[var(--color-ink-dim)]">
											{membersLabel(group.members)}
										</span>
									</div>
									<div class="flex shrink-0 items-center gap-1.5">
										<CopyMenu server={groupSummary(group)} />
										{#if confirmDeleteGroup === group.name}
											<button
												type="button"
												onclick={() => handleDeleteGroup(group.name)}
												disabled={deletingGroup === group.name}
												aria-busy={deletingGroup === group.name}
												class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
												style="background-color: var(--color-state-failed);"
											>
												Delete
											</button>
											<button
												type="button"
												onclick={() => (confirmDeleteGroup = null)}
												disabled={deletingGroup === group.name}
												class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-50"
											>
												Cancel
											</button>
										{:else}
											<button
												type="button"
												onclick={() => (confirmDeleteGroup = group.name)}
												aria-label={`Delete group ${group.name}`}
												class="shrink-0 rounded-lg border px-3 py-1.5 text-xs font-medium transition active:translate-y-px"
												style="border-color: color-mix(in oklab, var(--color-state-failed) 35%, transparent); color: var(--color-state-failed);"
											>
												Delete
											</button>
										{/if}
									</div>
								</div>
								{#if confirmDeleteGroup === group.name}
									<p class="text-[11px] leading-tight text-[var(--color-state-failed)]">
										This takes <code class="font-mono">{group.url}</code> offline and revokes tokens
										scoped to this group.
									</p>
								{/if}
								<code class="min-w-0 truncate font-mono text-[11px] text-[var(--color-ink-dim)]">
									{group.url}
								</code>
							</li>
						{/each}
					</ul>
				{/if}

				<!-- New group -->
				<form
					onsubmit={handleCreateGroup}
					class="flex flex-col gap-2 rounded-lg border border-dashed border-[var(--color-line)] p-3"
				>
					<div class="flex flex-wrap items-end gap-2">
						<div class="flex min-w-0 flex-1 basis-40 flex-col gap-1.5">
							<label for="group-name" class="text-xs font-medium text-[var(--color-ink-muted)]">
								New group
							</label>
							<input
								id="group-name"
								type="text"
								bind:value={newGroupName}
								autocomplete="off"
								spellcheck="false"
								placeholder="e.g. all, dev-tools"
								class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
							/>
						</div>
						<button
							type="submit"
							disabled={!groupNameValid || savingGroup}
							aria-busy={savingGroup}
							class="inline-flex shrink-0 items-center gap-2 rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)] disabled:cursor-not-allowed disabled:opacity-50"
						>
							{groups.some((g) => g.name === newGroupName.trim()) ? 'Update group' : 'Create group'}
						</button>
					</div>
					{#if newGroupName.trim().length > 0 && !groupNameValid}
						<p class="text-[11px] text-[var(--color-state-failed)]">
							Use lowercase letters, digits, and single hyphens (the URL routing key).
						</p>
					{/if}
					{#if confirmReplaceName !== null && confirmReplaceName === newGroupName.trim()}
						<div
							role="alert"
							class="flex flex-col gap-2.5 rounded-lg border p-3"
							style="border-color: color-mix(in oklab, var(--color-state-starting) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-starting) 8%, transparent);"
						>
							<p class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
								A group named <code class="font-mono text-[var(--color-ink)]">{confirmReplaceName}</code>
								already exists. Replace its members with your selection above? Its
								<code class="font-mono">/g/{confirmReplaceName}/mcp</code> endpoint stays up and its
								tokens keep working — only the membership changes.
							</p>
							<div class="flex items-center gap-1.5">
								<button
									type="button"
									onclick={() => submitGroup()}
									disabled={savingGroup}
									aria-busy={savingGroup}
									class="inline-flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)] disabled:cursor-wait disabled:opacity-70"
								>
									Replace members
								</button>
								<button
									type="button"
									onclick={() => (confirmReplaceName = null)}
									disabled={savingGroup}
									class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:opacity-50"
								>
									Cancel
								</button>
							</div>
						</div>
					{/if}

					<div class="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Group members">
						{#each [
							{ value: 'all', label: 'All servers', hint: 'every registered server, including future ones' },
							{ value: 'selected', label: 'Selected servers', hint: 'only the servers you pick below' }
						] as const as choice (choice.value)}
							<label
								class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
								style={newGroupMode === choice.value
									? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
									: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
							>
								<span class="flex items-center gap-2">
									<input
										type="radio"
										name="new-group-mode"
										value={choice.value}
										checked={newGroupMode === choice.value}
										onchange={() => (newGroupMode = choice.value)}
										class="sr-only"
									/>
									<span
										class="text-sm font-semibold"
										style={newGroupMode === choice.value
											? 'color: var(--color-accent);'
											: 'color: var(--color-ink);'}
									>
										{choice.label}
									</span>
								</span>
								<span class="text-[11px] leading-tight text-[var(--color-ink-dim)]">
									{choice.hint}
								</span>
							</label>
						{/each}
					</div>
					{#if newGroupMode === 'selected'}
						{#if servers.length === 0}
							<p class="text-xs text-[var(--color-ink-dim)]">No servers yet — add one first.</p>
						{:else}
							<div
								class="flex flex-col gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] p-2"
							>
								{#each servers as server (server.id)}
									<label
										class="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-[var(--color-surface)]"
										class:opacity-60={!server.enabled}
									>
										<input
											type="checkbox"
											checked={newGroupSelection.includes(server.id)}
											onchange={(e) => toggleNewGroupServer(server.id, e.currentTarget.checked)}
											class="size-4 shrink-0 accent-[var(--color-accent)]"
										/>
										<span class="min-w-0 flex-1 truncate text-sm text-[var(--color-ink)]">
											{serverLabel(server)}
										</span>
										{#if !server.enabled}
											<span class="text-[10px] text-[var(--color-ink-dim)]">disabled</span>
										{/if}
									</label>
								{/each}
							</div>
							<p class="text-xs text-[var(--color-ink-dim)]">
								Only running members appear in the bundle; a disabled pick joins when it starts.
							</p>
						{/if}
					{/if}
				</form>
			</fieldset>

			<!-- Control-plane auth -->
			<fieldset class="flex flex-col gap-2 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">Control-plane auth</legend>
				<div class="grid grid-cols-2 gap-2">
					{#each CONTROL_AUTH_CHOICES as choice (choice.value)}
						{@const locked = choice.value === 'always' && !hasUsableAdminCredential}
						<label
							class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
							class:cursor-not-allowed={locked}
							class:opacity-60={locked}
							style={settings.control_plane_auth === choice.value
								? 'border-color: color-mix(in oklab, var(--color-accent) 50%, transparent); background-color: color-mix(in oklab, var(--color-accent) 8%, transparent);'
								: 'border-color: var(--color-line); background-color: var(--color-surface-2);'}
						>
							<span class="flex items-center gap-2">
								<input
									type="radio"
									name="control-plane-auth"
									value={choice.value}
									checked={settings.control_plane_auth === choice.value}
									onchange={() => setControlPlaneAuth(choice.value)}
									disabled={savingField === 'control_plane_auth'}
									class="sr-only"
								/>
								<span
									class="font-mono text-sm font-semibold"
									style={settings.control_plane_auth === choice.value
										? 'color: var(--color-accent);'
										: 'color: var(--color-ink);'}
								>
									{choice.label}
								</span>
							</span>
							<span class="text-[11px] leading-tight text-[var(--color-ink-dim)]">
								{choice.hint}
							</span>
						</label>
					{/each}
				</div>
				<p class="text-xs text-[var(--color-ink-dim)]">
					When required, <code class="font-mono">/api</code> needs an admin (control-scope)
					token; the SPA logs in with it.
					{#if !hasUsableAdminCredential}
						<span class="text-[var(--color-accent)]">
							Generate an admin token above to enable
							<code class="font-mono">expose</code> / <code class="font-mono">always</code>.
						</span>
					{/if}
				</p>
			</fieldset>

			<!-- Allowed hosts -->
			<fieldset class="flex flex-col gap-2 border-0 p-0">
				<legend class="text-sm font-medium text-[var(--color-ink)]">
					Allowed hosts
					<span class="font-normal text-[var(--color-ink-dim)]">
						({settings.bind_mode === 'expose' ? 'enforced' : 'inactive while local'})
					</span>
				</legend>

				<form onsubmit={addHost} class="flex items-center gap-2">
					<input
						type="text"
						bind:value={newHost}
						autocomplete="off"
						spellcheck="false"
						placeholder="mcp.example.com"
						aria-label="Add allowed host"
						class="min-w-0 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
					/>
					<button
						type="submit"
						disabled={newHost.trim().length === 0 || savingField === 'allowed_hosts'}
						class="inline-flex shrink-0 items-center gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)] disabled:cursor-not-allowed disabled:opacity-50"
					>
						<svg
							class="size-3.5"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.5"
							stroke-linecap="round"
							aria-hidden="true"
						>
							<path d="M12 5v14M5 12h14" />
						</svg>
						Add
					</button>
				</form>

				{#if settings.allowed_hosts.length === 0}
					<p
						class="rounded-lg border border-dashed border-[var(--color-line)] px-3 py-3 text-xs text-[var(--color-ink-dim)]"
					>
						No hosts added. Only loopback is allowed when exposed.
					</p>
				{:else}
					<ul class="flex flex-wrap gap-2">
						{#each settings.allowed_hosts as host (host)}
							<li
								class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] py-1 pr-1 pl-2.5"
							>
								<span class="font-mono text-xs text-[var(--color-ink)]">{host}</span>
								<button
									type="button"
									onclick={() => removeHost(host)}
									disabled={savingField === 'allowed_hosts'}
									aria-label={`Remove ${host}`}
									class="rounded-md p-1 text-[var(--color-ink-dim)] transition hover:text-[var(--color-state-failed)] disabled:opacity-50"
								>
									<svg
										class="size-3.5"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="2"
										stroke-linecap="round"
										aria-hidden="true"
									>
										<path d="M18 6 6 18M6 6l12 12" />
									</svg>
								</button>
							</li>
						{/each}
					</ul>
				{/if}

				{#if confirmRemoveHost !== null}
					<div
						role="alert"
						class="flex flex-col gap-2.5 rounded-lg border p-3"
						style="border-color: color-mix(in oklab, var(--color-state-starting) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-starting) 8%, transparent);"
					>
						<p class="text-xs leading-relaxed text-[var(--color-ink-muted)]">
							Removing
							<code class="font-mono text-[var(--color-ink)]">{confirmRemoveHost}</code>
							will lock this browser out of the control plane — it's the host you're
							connected through. Open
							<a
								class="font-mono text-[var(--color-accent)] underline"
								href={loopbackUrl}
								target="_blank"
								rel="noopener noreferrer">{loopbackUrl}</a
							>
							first.
						</p>
						<div class="flex items-center gap-1.5">
							<button
								type="button"
								onclick={confirmRemoveCurrentHost}
								disabled={savingField !== null}
								class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition active:translate-y-px disabled:cursor-wait disabled:opacity-70"
								style="background-color: var(--color-state-failed);"
							>
								Remove anyway
							</button>
							<button
								type="button"
								onclick={() => (confirmRemoveHost = null)}
								class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
							>
								Cancel
							</button>
						</div>
					</div>
				{/if}
			</fieldset>
		</div>
	{/if}
</section>

{#if toast}
	<div class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
		<div class="w-full max-w-md">
			<Toast message={toast.message} tone={toast.tone} onclose={() => (toast = null)} />
		</div>
	</div>
{/if}
