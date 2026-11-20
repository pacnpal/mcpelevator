// A list setting edited as one entry per line. Lines, not commas: a group name can hold
// commas (an LDAP DN), and a save must never re-split a stored entry.

export function toLines(list: string[]): string {
	return list.join('\n');
}

export function fromLines(text: string): string[] {
	return text
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter(Boolean);
}
