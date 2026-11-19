/**
 * A bridge's uvicorn writes one access line per proxied request
 * (`INFO:     127.0.0.1:54141 - "POST /mcp HTTP/1.1" 200 OK`). Under real traffic
 * they bury everything else, so the log view can hide the successful ones. Error
 * responses (4xx/5xx) are never hidden: they are exactly what someone reading logs
 * is looking for.
 */
const ACCESS_LINE = /^(?:\S+\s+)?INFO:\s+\S+ - "[A-Z]+ \S+ HTTP\/[\d.]+" (\d{3})\b/;

export function isQuietAccessLine(line: string): boolean {
	const m = ACCESS_LINE.exec(line);
	return m !== null && Number(m[1]) < 400;
}
