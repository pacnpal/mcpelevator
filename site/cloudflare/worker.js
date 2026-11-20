// Serves pacnp.al/mcpelevator/* from the GitHub Pages copy at
// pacnpal.github.io/mcpelevator/*. It runs on a route, so it sits in front of
// whatever serves the rest of pacnp.al and leaves every other path alone.
const UPSTREAM = 'https://pacnpal.github.io';
const BASE = '/mcpelevator';

export default {
	async fetch(request) {
		const url = new URL(request.url);
		// Pages would redirect a bare /mcpelevator to its own host, so do it here.
		if (url.pathname === BASE) {
			return Response.redirect(`${url.origin}${BASE}/${url.search}`, 301);
		}
		if (!url.pathname.startsWith(BASE + '/')) return fetch(request);
		if (request.method !== 'GET' && request.method !== 'HEAD') {
			return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET, HEAD' } });
		}
		const res = await fetch(UPSTREAM + url.pathname + url.search, {
			method: request.method,
			headers: { Accept: request.headers.get('Accept') ?? '*/*' },
			redirect: 'manual',
			cf: { cacheTtl: 300, cacheEverything: true },
		});
		const out = new Response(res.body, res);
		const loc = out.headers.get('Location');
		if (loc) {
			// Compare parsed origins: a prefix check would also match pacnpal.github.io.evil.example.
			const target = new URL(loc, UPSTREAM);
			if (target.origin === UPSTREAM) {
				out.headers.set('Location', url.origin + target.pathname + target.search + target.hash);
			}
		}
		return out;
	},
};
