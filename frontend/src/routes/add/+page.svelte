<script lang="ts">
	import { goto } from '$app/navigation';
	import { createServer, errorMessage, importServers } from '$lib/api';
	import { canonicalRemoteTransport } from '$lib/remote';
	import type {
		ImportResult,
		ImportWarning,
		McpServerEntry,
		ServerCreate
	} from '$lib/types';
	import ServerForm from '$lib/components/ServerForm.svelte';
	import { flashToast } from '$lib/toast.svelte';

	type Tab = 'manual' | 'import';
	let tab = $state<Tab>('manual');

	// ---- Manual create --------------------------------------------------------
	let creating = $state(false);
	let createError = $state<string | null>(null);

	async function handleCreate(payload: ServerCreate) {
		if (creating) return;
		creating = true;
		createError = null;
		try {
			const created = await createServer(payload);
			await goto(`/server/${created.id}`);
		} catch (err) {
			createError = errorMessage(err);
			creating = false;
		}
	}

	// ---- Import ---------------------------------------------------------------
	const EXAMPLE = `{
  "mcpServers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"]
    }
  }
}`;

	let importText = $state('');
	let importing = $state(false);
	// Populated after an import that dropped docker run-options; keeps the operator on this page
	// to read the warnings before enabling. Cleared on the next import attempt.
	let importWarnings = $state<ImportWarning[]>([]);

	type PreviewEntry = {
		name: string;
		command: string;
		args: string[];
		skip: boolean;
		remote?: boolean;
		reason?: string;
	};

	// Entry shapes the backend treats as a remote server (mirrors import_mcp_servers).
	const REMOTE_TYPES = new Set(['sse', 'streamable-http', 'http']);

	type Parsed =
		| { ok: true; entries: PreviewEntry[]; payload: unknown }
		| { ok: false; error: string }
		| { empty: true };

	// Normalize both `{ mcpServers: {...} }` and a bare `{ name: {...} }` map.
	function extractMap(raw: unknown): Record<string, McpServerEntry> | null {
		if (!raw || typeof raw !== 'object') return null;
		const obj = raw as Record<string, unknown>;
		const inner =
			'mcpServers' in obj && obj.mcpServers && typeof obj.mcpServers === 'object'
				? (obj.mcpServers as Record<string, unknown>)
				: obj;
		// Validate every value is an object (entry-shaped).
		const out: Record<string, McpServerEntry> = {};
		for (const [k, v] of Object.entries(inner)) {
			if (!v || typeof v !== 'object') return null;
			out[k] = v as McpServerEntry;
		}
		return out;
	}

	const parsed = $derived<Parsed>(parseImport(importText));

	function parseImport(text: string): Parsed {
		const trimmed = text.trim();
		if (!trimmed) return { empty: true };
		let raw: unknown;
		try {
			raw = JSON.parse(trimmed);
		} catch (err) {
			return {
				ok: false,
				error: err instanceof Error ? err.message : 'Invalid JSON'
			};
		}
		const map = extractMap(raw);
		if (!map) {
			return {
				ok: false,
				error:
					'Expected a `mcpServers` object (or a bare name → config map).'
			};
		}
		const names = Object.keys(map);
		if (names.length === 0) {
			return { ok: false, error: 'No servers found in the JSON.' };
		}
		const entries: PreviewEntry[] = names.map((name) => {
			const e = map[name];
			// The backend importer accepts the transport under either `type` or `transport`.
			let type =
				typeof e.type === 'string'
					? e.type
					: typeof e.transport === 'string'
						? e.transport
						: undefined;
			// URL under `url` or Gemini CLI's `httpUrl` (Streamable-HTTP); a bare httpUrl
			// implies streamable-http. Mirrors import_mcp_servers.
			const url =
				typeof e.url === 'string'
					? e.url
					: typeof e.httpUrl === 'string'
						? e.httpUrl
						: undefined;
			if (type === undefined && typeof e.httpUrl === 'string' && typeof e.url !== 'string') {
				type = 'streamable-http';
			}
			// A remote (already-HTTP) entry is elevated into a proxied "remote" server.
			const looksRemote = !!url || (type !== undefined && REMOTE_TYPES.has(type.toLowerCase()));
			if (looksRemote) {
				if (!url) {
					return { name, command: '', args: [], skip: true, reason: 'Remote entry has no url' };
				}
				const transport = canonicalRemoteTransport(type);
				if (!transport) {
					return {
						name,
						command: url,
						args: [],
						skip: true,
						reason: `Unsupported remote transport "${type}"`
					};
				}
				return { name, command: url, args: [transport], skip: false, remote: true };
			}
			return {
				name,
				command: e.command ?? '',
				args: Array.isArray(e.args) ? e.args : [],
				skip: !e.command,
				reason: !e.command ? 'No command specified' : undefined
			};
		});
		return { ok: true, entries, payload: raw };
	}

	const creatableCount = $derived(
		parsed && 'ok' in parsed && parsed.ok
			? parsed.entries.filter((e) => !e.skip).length
			: 0
	);

	async function handleImport() {
		if (importing) return;
		if (!('ok' in parsed) || !parsed.ok) return;
		importing = true;
		importWarnings = [];
		try {
			const result: ImportResult = await importServers(parsed.payload);
			summarizeImport(result);
			if (result.warnings && result.warnings.length > 0) {
				// The hardened runner dropped some pasted docker options. Stay on the page and
				// show them so the operator reads them BEFORE going off to enable the server.
				importWarnings = result.warnings;
				importing = false;
			} else {
				await goto('/');
			}
		} catch (err) {
			flashToast(errorMessage(err), 'error');
			importing = false;
		}
	}

	function summarizeImport(result: ImportResult) {
		const c = result.created.length;
		const s = result.skipped.length;
		let msg = c === 1 ? 'Imported 1 server' : `Imported ${c} servers`;
		if (s > 0) {
			const reasons = result.skipped
				.map((sk) => `${sk.name} (${sk.reason})`)
				.join(', ');
			msg += ` · skipped ${s}: ${reasons}`;
		}
		flashToast(msg, c > 0 ? 'info' : 'error');
	}

	function loadExample() {
		importText = EXAMPLE;
	}
</script>

<section class="mx-auto flex w-full max-w-2xl flex-col gap-7">
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

	<!-- Heading -->
	<div>
		<h1 class="text-2xl font-semibold tracking-tight text-[var(--color-ink)]">
			Add a server
		</h1>
		<p class="mt-1 text-sm text-[var(--color-ink-muted)]">
			Point mcpelevator at a runner, or import an existing
			<code class="font-mono text-xs">mcpServers</code> config.
		</p>
	</div>

	<!-- Tabs -->
	<div
		class="inline-flex w-full gap-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] p-1 sm:w-auto sm:self-start"
		role="tablist"
		aria-label="Add server method"
	>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'manual'}
			onclick={() => (tab = 'manual')}
			class="flex-1 rounded-md px-4 py-1.5 text-sm font-medium transition sm:flex-none"
			style={tab === 'manual'
				? 'background-color: var(--color-elevated); color: var(--color-ink);'
				: 'color: var(--color-ink-muted);'}
		>
			Manual
		</button>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'import'}
			onclick={() => (tab = 'import')}
			class="flex-1 rounded-md px-4 py-1.5 text-sm font-medium transition sm:flex-none"
			style={tab === 'import'
				? 'background-color: var(--color-elevated); color: var(--color-ink);'
				: 'color: var(--color-ink-muted);'}
		>
			Import JSON
		</button>
	</div>

	{#if tab === 'manual'}
		<ServerForm
			mode="create"
			busy={creating}
			error={createError}
			onsubmit={handleCreate}
			oncancel={() => goto('/')}
		/>
	{:else}
		<!-- Import tab -->
		<div class="flex flex-col gap-4">
			<div class="flex items-center justify-between gap-3">
				<label
					for="import-json"
					class="text-sm font-medium text-[var(--color-ink)]"
				>
					Paste <code class="font-mono text-xs">mcpServers</code> JSON
				</label>
				<button
					type="button"
					onclick={loadExample}
					class="text-xs font-medium text-[var(--color-accent)] transition hover:text-[var(--color-accent-strong)]"
				>
					Insert example
				</button>
			</div>

			<textarea
				id="import-json"
				bind:value={importText}
				rows="10"
				spellcheck="false"
				placeholder={EXAMPLE}
				class="resize-y rounded-lg border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3 py-2.5 font-mono text-xs leading-relaxed text-[var(--color-ink)] outline-none transition placeholder:text-[var(--color-ink-dim)] focus:border-[var(--color-line-strong)]"
			></textarea>

			<!-- Parse feedback / preview -->
			{#if 'error' in parsed}
				<p
					role="alert"
					class="rounded-lg border px-3.5 py-3 font-mono text-xs leading-relaxed"
					style="border-color: color-mix(in oklab, var(--color-state-failed) 35%, transparent); background-color: color-mix(in oklab, var(--color-state-failed) 10%, transparent); color: var(--color-state-failed);"
				>
					{parsed.error}
				</p>
			{:else if 'ok' in parsed && parsed.ok}
				<div class="flex flex-col gap-2">
					<p class="text-xs text-[var(--color-ink-muted)]">
						{creatableCount} of {parsed.entries.length} will be created
					</p>
					<ul class="flex flex-col gap-2">
						{#each parsed.entries as entry (entry.name)}
							<li
								class="flex flex-col gap-1 rounded-lg border px-3.5 py-2.5"
								style={entry.skip
									? 'border-color: var(--color-line); background-color: var(--color-surface); opacity: 0.7;'
									: 'border-color: color-mix(in oklab, var(--color-accent) 28%, transparent); background-color: color-mix(in oklab, var(--color-accent) 6%, transparent);'}
							>
								<div class="flex items-center justify-between gap-2">
									<span
										class="truncate text-sm font-medium text-[var(--color-ink)]"
									>
										{entry.name}
									</span>
									{#if entry.skip}
										<span
											class="shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium tracking-wide text-[var(--color-ink-dim)]"
											style="border-color: var(--color-line-strong);"
										>
											SKIPPED
										</span>
									{:else}
										<svg
											class="size-4 shrink-0 text-[var(--color-accent)]"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="2.5"
											stroke-linecap="round"
											stroke-linejoin="round"
											aria-hidden="true"
										>
											<path d="M20 6 9 17l-5-5" />
										</svg>
									{/if}
								</div>
								{#if entry.skip}
									<p class="text-[11px] text-[var(--color-ink-dim)]">
										{entry.reason}
									</p>
								{:else}
									<code
										class="truncate font-mono text-[11px] text-[var(--color-ink-muted)]"
									>
										{#if entry.remote}
											remote → {entry.command} ({entry.args.join(' ')})
										{:else}
											{entry.command}
											{entry.args.join(' ')}
										{/if}
									</code>
								{/if}
							</li>
						{/each}
					</ul>
				</div>
			{:else}
				<p
					class="rounded-lg border border-dashed border-[var(--color-line)] px-3.5 py-3 text-xs text-[var(--color-ink-dim)]"
				>
					Paste a config to preview what will be imported. Entries with a
					<code class="font-mono">url</code> (remote servers) are imported as proxied
					remote endpoints; only malformed entries are skipped.
				</p>
			{/if}

			<!-- Post-import warnings: docker run-options the hardened runner dropped. Shown here
			     (instead of navigating away) so the operator reviews them before enabling. -->
			{#if importWarnings.length > 0}
				<div
					role="alert"
					class="rounded-lg border px-3.5 py-3 text-xs"
					style="border-color: color-mix(in oklab, var(--color-state-unhealthy) 45%, transparent); background-color: color-mix(in oklab, var(--color-state-unhealthy) 8%, transparent);"
				>
					<p class="font-semibold text-[var(--color-ink)]">
						Imported — but some Docker options were dropped by the hardened runner. Review
						before enabling:
					</p>
					<ul class="mt-2 flex flex-col gap-2">
						{#each importWarnings as w (w.name)}
							<li>
								<span class="font-mono text-[var(--color-ink)]">{w.name}</span>
								<ul class="mt-1 list-disc pl-5 text-[var(--color-ink-muted)]">
									{#each w.warnings as line}
										<li>{line}</li>
									{/each}
								</ul>
							</li>
						{/each}
					</ul>
					<div class="mt-3 flex justify-end">
						<button
							type="button"
							onclick={() => goto('/')}
							class="rounded-lg bg-[var(--color-accent)] px-3.5 py-1.5 text-xs font-semibold text-[var(--color-accent-ink)] transition hover:bg-[var(--color-accent-strong)]"
						>
							Go to servers
						</button>
					</div>
				</div>
			{/if}

			<!-- Actions -->
			<div class="flex items-center justify-end gap-2 pt-1">
				<button
					type="button"
					onclick={() => goto('/')}
					class="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2 text-sm font-medium text-[var(--color-ink-muted)] transition hover:border-[var(--color-line-strong)] hover:text-[var(--color-ink)]"
				>
					Cancel
				</button>
				<button
					type="button"
					onclick={handleImport}
					disabled={importing || !('ok' in parsed && parsed.ok) || creatableCount === 0}
					aria-busy={importing}
					class="inline-flex items-center gap-2 rounded-lg bg-[var(--color-accent)] px-5 py-2 text-sm font-semibold text-[var(--color-accent-ink)] transition active:translate-y-px hover:bg-[var(--color-accent-strong)] disabled:cursor-not-allowed disabled:opacity-50"
				>
					{#if importing}
						<svg class="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
							<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-opacity="0.25" />
							<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" />
						</svg>
						Importing
					{:else}
						Import {creatableCount > 0 ? `${creatableCount} server${creatableCount === 1 ? '' : 's'}` : ''}
					{/if}
				</button>
			</div>
		</div>
	{/if}
</section>

