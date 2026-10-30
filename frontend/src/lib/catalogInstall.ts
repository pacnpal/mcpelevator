// Hand-off for the catalog → install review flow.
//
// The /catalog page resolves a chosen draft and stashes it here, then navigates
// to /catalog/install which renders the pre-filled ServerForm. Keeping the draft
// in a module-level store (not the URL) keeps it strongly typed and avoids
// serializing env/args through query params. It's intentionally ephemeral: a hard
// refresh of /catalog/install clears it, and that page bounces back to /catalog.

import type { CatalogDetail, CatalogSource, Runner, ServerCreate } from './types';

export interface PendingInstall {
	/** Pre-filled form values derived from the catalog draft. */
	initial: Partial<ServerCreate>;
	/** Provenance to stamp on create, e.g. `catalog:io.example/srv@1.0.0`. */
	source: string;
	/** Where this came from, for the review header. */
	sourceLabel: string;
	installSupport: CatalogSource['install_support'];
	/** Required/secret values or other notes the operator should resolve. */
	warnings: string[];
	notes: string[];
	/** Link back to the upstream listing / repo, when available. */
	repositoryUrl: string | null;
	webUrl: string | null;
	/** Where "back" and Cancel lead; the catalog when unset. */
	back?: { href: string; label: string };
}

let pending: PendingInstall | null = null;

/**
 * Builds the review hand-off for a resolved detail's local-package draft: the first
 * installable draft, else a manual scaffold that carries the reason (and any remote
 * endpoints) so the form is explained rather than mysteriously blank.
 *
 * @param detail - The resolved catalog (or repository) detail
 * @param origin - Provenance to stamp on create, the review header's label, and the back link
 * @returns The pending install to stash before navigating to the review form
 */
export function pendingFromDraft(
	detail: CatalogDetail,
	origin: { source: string; sourceLabel: string; back?: PendingInstall['back'] }
): PendingInstall {
	const installable = detail.drafts.find((d) => d.installable);
	const draft = installable ?? detail.drafts[0] ?? null;
	const autoInstallable = !!draft?.installable;

	const notes = [...detail.notes];
	if (!autoInstallable && draft?.reason) notes.push(draft.reason);
	for (const r of detail.remotes) {
		notes.push(`Remote ${r.type} endpoint (not a local runner): ${r.url}`);
	}

	const warnings = draft?.warnings ?? [];
	return {
		initial: {
			name: detail.server.title || detail.server.name,
			runner: (draft?.runner ?? 'npx') as Runner,
			command: draft?.command ?? '',
			args: draft?.args ?? [],
			env: draft?.env ?? {},
			// A repository install's checkout + detected build steps (blank for packages).
			repo_url: draft?.repo_url ?? '',
			repo_ref: draft?.repo_ref ?? '',
			post_clone_script: draft?.post_clone_script ?? '',
			// Only auto-start a clean, fully-resolved install; anything with
			// required/secret/placeholder values or no runnable command must be
			// reviewed first, so don't boot a broken server.
			enabled: autoInstallable && warnings.length === 0
		},
		...origin,
		installSupport: detail.manual_install || !autoInstallable ? 'manual' : 'auto',
		warnings,
		notes,
		repositoryUrl: detail.server.repository_url,
		webUrl: detail.server.web_url
	};
}

/**
 * Stores a pending install hand-off.
 *
 * @param value - The pending install data to retain for the next review step
 */
export function setPendingInstall(value: PendingInstall): void {
	pending = value;
}

/**
 * Retrieves the pending install and clears the hand-off store.
 *
 * @returns The stored pending install, or `null` if none is set.
 */
export function takePendingInstall(): PendingInstall | null {
	const value = pending;
	pending = null;
	return value;
}

/** Repo/web links come from public directory metadata (untrusted). Only render an
 * anchor when the URL parses as http(s), so a malicious entry can't slip a
 * `javascript:` (or other unsafe-scheme) href into the page. */
export function safeUrl(url: string | null): string | null {
	if (!url) return null;
	try {
		const u = new URL(url);
		return u.protocol === 'http:' || u.protocol === 'https:' ? url : null;
	} catch {
		return null;
	}
}
