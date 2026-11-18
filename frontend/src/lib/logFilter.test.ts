import { describe, expect, it } from 'vitest';
import { isQuietAccessLine } from './logFilter';

describe('isQuietAccessLine', () => {
	it('matches successful uvicorn access lines', () => {
		expect(isQuietAccessLine('INFO:     127.0.0.1:54141 - "POST /mcp HTTP/1.1" 200 OK')).toBe(true);
		expect(isQuietAccessLine('INFO:     127.0.0.1:5 - "GET /mcp HTTP/1.1" 202 Accepted')).toBe(true);
		expect(isQuietAccessLine('INFO:     127.0.0.1:5 - "DELETE /mcp HTTP/1.1" 307 Temporary Redirect')).toBe(true);
	});

	it('keeps error responses visible', () => {
		expect(isQuietAccessLine('INFO:     127.0.0.1:5 - "POST /mcp HTTP/1.1" 404 Not Found')).toBe(false);
		expect(isQuietAccessLine('INFO:     127.0.0.1:5 - "POST /mcp HTTP/1.1" 500 Internal Server Error')).toBe(false);
	});

	it('keeps everything that is not an access line', () => {
		expect(isQuietAccessLine('[mcpelevator] attempt 1/1: readiness')).toBe(false);
		expect(isQuietAccessLine('INFO:     Uvicorn running on http://127.0.0.1:49205')).toBe(false);
		expect(isQuietAccessLine('Error: ENOENT /data')).toBe(false);
	});
});
