// Hand-off for the catalog → install review flow.
//
// The /catalog page resolves a chosen draft and stashes it here, then navigates
// to /catalog/install which renders the pre-filled ServerForm. Keeping the draft
// in a module-level store (not the URL) keeps it strongly typed and avoids
// serializing env/args through query params. It's intentionally ephemeral: a hard
// refresh of /catalog/install clears it, and that page bounces back to /catalog.

import type { CatalogDraft, CatalogSource, ServerCreate } from './types';

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
}

let pending: PendingInstall | null = null;

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
