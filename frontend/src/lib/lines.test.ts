import { describe, expect, it } from 'vitest';

import { fromLines, toLines } from './lines.js';

describe('one entry per line', () => {
	it('keeps a stored LDAP DN whole through an unrelated save', () => {
		const stored = ['CN=admins,OU=Groups,DC=example,DC=com', 'staff'];
		// The form shows the list, the admin edits some other SSO field, the page saves.
		expect(fromLines(toLines(stored))).toEqual(stored);
	});

	it('trims entries and drops blank lines', () => {
		expect(fromLines('  staff \r\n\n mcp-admins\n')).toEqual(['staff', 'mcp-admins']);
	});
});
