// node site/cloudflare/worker.test.mjs — checks the routing logic with a stubbed fetch.
import assert from 'node:assert/strict';
import worker from './worker.js';

const calls = [];
globalThis.fetch = async (input, init) => {
	const url = typeof input === 'string' ? input : input.url;
	calls.push(url);
	if (url.endsWith('/mcpelevator/old')) {
		return new Response(null, { status: 301, headers: { Location: 'https://pacnpal.github.io/mcpelevator/new/' } });
	}
	if (url.endsWith('/mcpelevator/lookalike')) {
		return new Response(null, { status: 302, headers: { Location: 'https://pacnpal.github.io.evil.example/x' } });
	}
	return new Response('ok:' + url, { status: 200, headers: { 'content-type': 'text/html' } });
};
const get = (path, method = 'GET') => worker.fetch(new Request('https://pacnp.al' + path, { method }));

let r = await get('/mcpelevator?x=1');
assert.equal(r.status, 301);
assert.equal(r.headers.get('Location'), 'https://pacnp.al/mcpelevator/?x=1');

r = await get('/mcpelevator/screenshots/servers.png');
assert.equal(r.status, 200);
assert.equal(calls.at(-1), 'https://pacnpal.github.io/mcpelevator/screenshots/servers.png');

r = await get('/mcpelevator/old');
assert.equal(r.headers.get('Location'), 'https://pacnp.al/mcpelevator/new/');

// A lookalike host is left alone, never rewritten onto pacnp.al.
r = await get('/mcpelevator/lookalike');
assert.equal(r.headers.get('Location'), 'https://pacnpal.github.io.evil.example/x');

r = await get('/mcpelevator/', 'POST');
assert.equal(r.status, 405);

r = await get('/mcpelevatorx');
assert.equal(calls.at(-1), 'https://pacnp.al/mcpelevatorx');

console.log('worker ok');
