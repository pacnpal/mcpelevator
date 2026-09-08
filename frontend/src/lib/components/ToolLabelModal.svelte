<script lang="ts">
	// The one place a tool's exposed name and description are edited. The tool list only
	// opens this dialog — no inline editor — so an operator never has to scroll past a
	// wall of upstream prose to reach the fields, and the edit form exists once.
	//
	// The dialog holds a LOCAL draft: typing changes nothing until Save, which hands the
	// override back to the caller to stage. The staged batch is still what an Apply
	// writes (one PATCH, one bridge restart) — this only moves where the typing happens.
	import type { ToolOverride } from '$lib/types';

	let {
		upstreamName,
		override,
		servedDescription,
		restoringDescription = false,
		takenNames = new Set<string>(),
		disabled = false,
		onsave,
		onclose
	}: {
		/** The tool's stable identity — its name upstream, before any rename. */
		upstreamName: string;
		/** The override staged so far ({} when the tool is unlabelled). */
		override: ToolOverride;
		/** What clients currently see as the description — the upstream's, or a saved
		 * override. Shown as the placeholder: leaving the field empty keeps it. */
		servedDescription?: string;
		/** True when a saved description override is staged for removal, so the served
		 * text is the very thing being cleared and must not be offered as "current". */
		restoringDescription?: boolean;
		/** Names other exposed tools already answer to — renaming onto one is refused by
		 * the bridge, so warn here, where the operator can still change it. */
		takenNames?: Set<string>;
		/** True while an Apply or a lifecycle action is in flight; the fields go read-only. */
		disabled?: boolean;
		/** The edited override, already trimmed with empty fields dropped. */
		onsave?: (next: ToolOverride) => void;
		onclose?: () => void;
	} = $props();

	// Draft state, seeded once per open — deliberately a SNAPSHOT of the staged override,
	// not a live view of it: the dialog owns the draft until Save, and the caller remounts
	// it per tool, so re-reading `override` later would fight the operator's typing.
	// svelte-ignore state_referenced_locally
	let name = $state(override.name ?? '');
	// svelte-ignore state_referenced_locally
	let description = $state(override.description ?? '');

	let dialogEl = $state<HTMLDialogElement>();
	let nameEl = $state<HTMLInputElement>();

	const trimmedName = $derived(name.trim());
	const exposedName = $derived(trimmedName || upstreamName);
	const collides = $derived(exposedName !== upstreamName && takenNames.has(exposedName));

	/** The draft as the backend would store it: trimmed, with blank fields dropped so
	 * clearing one reads as "keep the upstream's", not as an empty override. */
	function draft(): ToolOverride {
		const next: ToolOverride = {};
		if (trimmedName) next.name = trimmedName;
		if (description.trim()) next.description = description.trim();
		return next;
	}

	/** The single exit path: close the native dialog, whose `close` event tells the
	 *  caller. Falls back to notifying directly if the element isn't there to close. */
	function dismiss() {
		if (dialogEl?.open) dialogEl.close();
		else onclose?.();
	}

	function save() {
		if (disabled) return;
		onsave?.(draft());
		dismiss();
	}

	// `showModal` is what makes this a real modal: the browser traps Tab inside the
	// dialog, makes the rest of the page inert (so the lifecycle and tool controls
	// underneath can't be focused or activated through it), handles Escape, and returns
	// focus to the button that opened it on close. Then focus the first field, so the
	// editor is usable from the keyboard alone.
	$effect(() => {
		if (dialogEl && !dialogEl.open) dialogEl.showModal();
		nameEl?.focus();
		nameEl?.select();
	});
</script>

<!-- A native modal dialog: the browser owns the focus trap, the inert background,
     Escape, and returning focus to the opener — a hand-rolled overlay owns none of that.
     A click that lands on the dialog element itself is a backdrop click (the panel below
     covers the rest), so it dismisses. -->
<dialog
	bind:this={dialogEl}
	onclose={() => onclose?.()}
	onclick={(e) => {
		if (e.target === dialogEl) dismiss();
	}}
	class="m-auto w-full max-w-lg bg-transparent p-0 text-[var(--color-ink)] backdrop:bg-black/50"
>
	<div
		class="flex w-full flex-col gap-4 rounded-[var(--radius-card)] border border-[var(--color-line-strong)] bg-[var(--color-elevated)] p-5 shadow-2xl"
	>
		<div class="flex items-start justify-between gap-3">
			<div class="min-w-0">
				<h2 id="tool-label-modal-title" class="text-sm font-semibold text-[var(--color-ink)]">
					Edit tool labels
				</h2>
				<p class="mt-0.5 truncate font-mono text-xs text-[var(--color-ink-dim)]">
					{upstreamName}
				</p>
			</div>
			<button
				type="button"
				onclick={dismiss}
				aria-label="Close"
				class="rounded-md p-1 text-[var(--color-ink-dim)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
			>
				<svg
					class="size-4"
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
		</div>

		<label class="flex flex-col gap-1">
			<span class="text-[11px] font-medium text-[var(--color-ink-muted)]">Name</span>
			<input
				bind:this={nameEl}
				type="text"
				bind:value={name}
				placeholder={upstreamName}
				spellcheck="false"
				autocomplete="off"
				{disabled}
				onkeydown={(e) => {
					if (e.key === 'Enter') {
						e.preventDefault();
						save();
					}
				}}
				class="rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-2 font-mono text-xs text-[var(--color-ink)] outline-none focus:border-[var(--color-accent)] disabled:opacity-50"
			/>
		</label>

		<label class="flex flex-col gap-1">
			<span class="text-[11px] font-medium text-[var(--color-ink-muted)]">Description</span>
			<textarea
				rows="8"
				bind:value={description}
				placeholder={restoringDescription || !servedDescription
					? "The upstream's description"
					: servedDescription}
				{disabled}
				onkeydown={(e) => {
					// Enter stays a newline here; the usual multi-line shortcut saves.
					if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
						e.preventDefault();
						save();
					}
				}}
				class="resize-y rounded-md border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-2 text-xs leading-relaxed text-[var(--color-ink)] outline-none focus:border-[var(--color-accent)] disabled:opacity-50"
			></textarea>
		</label>

		{#if collides}
			<p class="text-[11px] leading-snug" style="color: var(--color-state-failed);" role="alert">
				Another exposed tool already answers to
				<code class="font-mono">{exposedName}</code> — the bridge will refuse this rename.
			</p>
		{/if}

		<p class="text-[11px] leading-relaxed text-[var(--color-ink-dim)]">
			Leave a field empty to keep the upstream's. A renamed tool answers to its new name
			only — clients using the old one must be updated. Saving here stages the change;
			<strong>Apply</strong> writes it in one restart.
		</p>

		<div class="flex items-center justify-end gap-2">
			<button
				type="button"
				onclick={dismiss}
				class="rounded-md border border-[var(--color-line)] px-3 py-1.5 text-xs font-medium text-[var(--color-ink-muted)] transition hover:text-[var(--color-ink)]"
			>
				Cancel
			</button>
			<button
				type="button"
				onclick={save}
				{disabled}
				class="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-[var(--color-accent-ink)] transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
			>
				Save
			</button>
		</div>
	</div>
</dialog>
