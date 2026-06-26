import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	kit: {
		// SPA mode: every route falls back to index.html, which the FastAPI
		// backend serves as the catch-all for non-API paths. No server-side
		// rendering happens — the app is rendered entirely in the browser.
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: 'index.html',
			precompress: false,
			strict: false
		})
	},
	compilerOptions: {
		// Force runes mode everywhere except node_modules (libraries opt out).
		// Can be removed once Svelte 6 defaults to runes.
		runes: true
	}
};

export default config;
