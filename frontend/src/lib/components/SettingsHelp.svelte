<script lang="ts">
	// The Settings page's Help section: how to connect a client, where common jobs live in
	// the UI, first steps when something breaks, and the docs. Links into Settings use
	// `?tab=`, and admin-only destinations are left out for members.
	let {
		baseUrl,
		isAdmin
	}: {
		/** The instance's public base, e.g. https://mcp.example.com. */
		baseUrl: string;
		isAdmin: boolean;
	} = $props();

	const REPO = 'https://github.com/pacnpal/mcpelevator';
	const README = `${REPO}#readme`;

	const endpoints = $derived([
		{ path: '/s/<slug>/mcp', what: 'One server, by the slug shown on its card.' },
		{ path: '/g/<name>/mcp', what: "A group: its running members' tools in one bundle, prefixed by slug." },
		{ path: '/s/mcpelevator/mcp', what: 'The built-in server: this control plane as MCP tools.' }
	]);

	const tasks = $derived(
		[
			{ href: '/add', label: 'Add a server', hint: 'From a command, a URL, a JSON config or a GitHub repo', admin: false },
			{ href: '/catalog', label: 'Install from the catalog', hint: 'Browse public MCP directories', admin: false },
			{ href: '/settings', label: 'Create an access token', hint: 'For servers set to bearer auth', admin: false },
			{ href: '/settings?tab=groups', label: 'Bundle servers into a group', hint: 'One URL for several servers', admin: true },
			{ href: '/settings?tab=users', label: 'Invite a teammate', hint: 'Add a user and mint a login token', admin: true },
			{ href: '/settings?tab=network', label: 'Reach this box from elsewhere', hint: 'Bind mode, LAN access, allowed hosts', admin: true },
			{ href: '/settings?tab=sso', label: 'Sign in with your identity provider', hint: 'OpenID Connect and SCIM', admin: true },
			{ href: '/usage', label: 'See which tools get called', hint: 'Per-server and per-tool usage', admin: false }
		].filter((t) => isAdmin || !t.admin)
	);

	const troubleshooting = [
		{
			q: "A server won't start",
			a: "Open it from the dashboard. The header shows its last error, and Logs shows the bridge's output, including the setup and checkout steps. Retry starts it again after you fix the cause."
		},
		{
			q: 'A client gets 401 or 403',
			a: "Check the server's auth (Edit → Auth). A bearer server takes a token scoped to all or to that server, and a group's URL one scoped to all or to that group. An OAuth server needs the authorization server set under Server auth."
		},
		{
			q: 'Locked out of the control plane',
			a: 'Loopback always works: open the UI at http://localhost on the box itself, or over an SSH tunnel. On first boot with auth on, the admin token is printed once to the container logs.'
		},
		{
			q: 'Need more detail in the logs',
			a: 'Set MCPE_DEBUG=true and restart. Every request, SQL statement and bridge logs at TRACE, with credentials scrubbed. Turn it off again afterwards.'
		}
	];

	const docs = [
		{ href: `${REPO}#adding-a-server`, label: 'Adding a server' },
		{ href: `${REPO}#security`, label: 'Security model' },
		{ href: `${REPO}/blob/main/docs/claude-web-exposure.md`, label: 'Exposing mcpelevator to Claude' },
		{ href: `${REPO}#configuration-env-vars-prefix-mcpe_`, label: 'Configuration (env vars)' },
		{ href: `${REPO}#route-reference`, label: 'Route reference' },
		{ href: `${REPO}/blob/main/docs/unraid.md`, label: 'Running on Unraid' }
	];

	const H3 = 'text-sm font-semibold text-[var(--color-ink)]';
	const CARD =
		'flex flex-col gap-3 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-5';
</script>

<div class="flex flex-col gap-5">
	<!-- Connect a client -->
	<section class={CARD} aria-labelledby="help-connect">
		<div class="flex flex-col gap-1">
			<h3 id="help-connect" class={H3}>Connect a client</h3>
			<p class="max-w-prose text-xs leading-relaxed text-[var(--color-ink-dim)]">
				Point any client that speaks Streamable HTTP at one of these URLs. A protected server
				or group also needs
				<code class="font-mono text-[var(--color-ink-muted)]">Authorization: Bearer &lt;token&gt;</code>.
				Each server card's copy menu has ready-made client configs.
			</p>
		</div>
		<ul class="flex flex-col divide-y divide-[var(--color-line)]">
			{#each endpoints as e (e.path)}
				<li class="flex flex-col gap-0.5 py-2.5 first:pt-0 last:pb-0">
					<code class="font-mono text-xs break-all text-[var(--color-ink)]"
						><span class="text-[var(--color-ink-dim)]">{baseUrl}</span>{e.path}</code
					>
					<span class="text-xs text-[var(--color-ink-dim)]">{e.what}</span>
				</li>
			{/each}
		</ul>
	</section>

	<!-- Common tasks -->
	<section class="flex flex-col gap-3" aria-labelledby="help-tasks">
		<h3 id="help-tasks" class={H3}>Common tasks</h3>
		<ul class="grid gap-2 sm:grid-cols-2">
			{#each tasks as t (t.href + t.label)}
				<li>
					<a
						href={t.href}
						data-sveltekit-noscroll={t.href.startsWith('/settings') ? '' : undefined}
						class="flex h-full flex-col gap-0.5 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-3 transition hover:border-[var(--color-line-strong)]"
					>
						<span class="text-sm font-medium text-[var(--color-ink)]">{t.label}</span>
						<span class="text-xs text-[var(--color-ink-dim)]">{t.hint}</span>
					</a>
				</li>
			{/each}
		</ul>
	</section>

	<!-- Troubleshooting -->
	<section class={CARD} aria-labelledby="help-trouble">
		<h3 id="help-trouble" class={H3}>Troubleshooting</h3>
		<div class="flex flex-col divide-y divide-[var(--color-line)]">
			{#each troubleshooting as item (item.q)}
				<details class="group py-2.5 first:pt-0 last:pb-0">
					<summary
						class="flex cursor-pointer list-none items-center justify-between gap-3 text-sm text-[var(--color-ink)] [&::-webkit-details-marker]:hidden"
					>
						{item.q}
						<svg
							class="size-3.5 shrink-0 text-[var(--color-ink-dim)] transition group-open:rotate-180"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="m6 9 6 6 6-6" />
						</svg>
					</summary>
					<p class="mt-2 max-w-prose text-xs leading-relaxed text-[var(--color-ink-muted)]">
						{item.a}
					</p>
				</details>
			{/each}
		</div>
	</section>

	<!-- Documentation -->
	<section class={CARD} aria-labelledby="help-docs">
		<div class="flex items-baseline justify-between gap-3">
			<h3 id="help-docs" class={H3}>Documentation</h3>
			<a
				href={README}
				target="_blank"
				rel="noopener noreferrer"
				class="text-xs text-[var(--color-ink-muted)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-ink)]"
				>Full README</a
			>
		</div>
		<ul class="grid gap-x-6 gap-y-2 sm:grid-cols-2">
			{#each docs as d (d.href)}
				<li>
					<a
						href={d.href}
						target="_blank"
						rel="noopener noreferrer"
						class="inline-flex items-center gap-1.5 text-sm text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
					>
						{d.label}
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
					</a>
				</li>
			{/each}
		</ul>
	</section>
</div>
