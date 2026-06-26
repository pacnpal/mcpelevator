<script lang="ts">
	import {
		createToken,
		deleteToken,
		errorMessage,
		getSettings,
		listTokens,
		updateSettings
	} from '$lib/api';
	import type {
		AuthProvider,
		BindMode,
		SettingsInfo,
		TokenCreated,
		TokenInfo
	} from '$lib/types';
	import CopyButton from '$lib/components/CopyButton.svelte';
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

	async function load() {
		loadState = 'loading';
		loadError = null;
		try {
			const [s, t] = await Promise.all([getSettings(), listTokens()]);
			settings = s;
			tokens = t;
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

	// New-token flow: name entry → POST → one-time plaintext reveal.
	let newTokenName = $state('');
	let creating = $state(false);
	let createdToken = $state<TokenCreated | null>(null);

	const newNameValid = $derived(newTokenName.trim().length > 0);

	async function handleCreateToken(e: SubmitEvent) {
		e.preventDefault();
		if (creating || !newNameValid) return;
		creating = true;
		try {
			const created = await createToken(newTokenName.trim());
			createdToken = created;
			// List it (by prefix) immediately; the reveal box holds the plaintext.
			const { token: _token, ...info } = created;
			void _token;
			tokens = [info, ...tokens];
			newTokenName = '';
		} catch (err) {
			flashToast(errorMessage(err));
		} finally {
			creating = false;
		}
	}

	function dismissReveal() {
		createdToken = null;
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

	function setDefaultAuth(value: AuthProvider) {
		if (!settings || settings.default_auth_provider === value) return;
		patchSettings({ default_auth_provider: value }, 'default_auth_provider');
	}

	function setBindMode(value: BindMode) {
		if (!settings || settings.bind_mode === value) return;
		patchSettings({ bind_mode: value }, 'bind_mode');
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

	function removeHost(host: string) {
		if (!settings) return;
		const next = settings.allowed_hosts.filter((h) => h !== host);
		patchSettings({ allowed_hosts: next }, 'allowed_hosts');
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
			Access tokens and security for the control plane.
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
					Bearer tokens for servers (or the API) set to <code class="font-mono">bearer</code> auth.
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
			<form onsubmit={handleCreateToken} class="flex items-end gap-2">
				<div class="flex min-w-0 flex-1 flex-col gap-1.5">
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
							<div class="flex min-w-0 flex-col gap-0.5">
								<span class="truncate text-sm font-medium text-[var(--color-ink)]">
									{token.name}
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
						<label
							class="flex cursor-pointer flex-col gap-1 rounded-lg border px-3 py-2.5 transition focus-within:ring-2 focus-within:ring-[var(--color-accent)]"
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
				<p class="text-xs text-[var(--color-ink-dim)]">
					<code class="font-mono">expose</code> enforces the Host/Origin allowlist below (loopback
					always allowed).
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
