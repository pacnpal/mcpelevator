import type { ClientRequest, IncomingMessage } from 'node:http';
import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import type { ProxyOptions } from 'vite';
import { defineConfig } from 'vitest/config';

// Dev server proxies API + streaming traffic to the FastAPI backend so the
// SPA can call same-origin paths (/api, /s) in development exactly as it will
// in production, where the backend serves the built static files.
const BACKEND = 'http://127.0.0.1:8080';

// Dev proxy configuration:
//
// 1. Rewrite the request Origin to the backend (loopback). When the dev UI is
//    opened from a LAN IP (mobile testing), `changeOrigin` rewrites Host to the
//    backend but the browser Origin stays `http://<lan-ip>:5173`, which the
//    backend's Host/Origin guard rejects. Rewriting Origin to loopback lets dev
//    requests through — it's the developer's own machine, and prod (the built SPA
//    served by FastAPI) is same-origin, so it is unaffected.
// 2. Strip buffering hints from streamed (SSE) responses so the proxy flushes
//    events to the client as they arrive.
const devProxy: ProxyOptions['configure'] = (proxy) => {
	// Rewrite Origin for both plain HTTP requests and WebSocket upgrades —
	// `proxyReq` does not fire for upgrades, so `proxyReqWs` is needed too.
	const rewriteOrigin = (proxyReq: ClientRequest) => proxyReq.setHeader('origin', BACKEND);
	proxy.on('proxyReq', rewriteOrigin);
	proxy.on('proxyReqWs', rewriteOrigin);
	proxy.on('proxyRes', (proxyRes: IncomingMessage) => {
		proxyRes.headers['cache-control'] = 'no-cache, no-transform';
		proxyRes.headers['x-accel-buffering'] = 'no';
	});
};

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	test: {
		environment: 'jsdom',
		environmentOptions: { jsdom: { url: 'http://localhost:5173/' } },
		setupFiles: ['./src/test-setup.ts'],
		globals: true,
		include: ['src/**/*.{test,spec}.ts']
	},
	server: {
		port: 5173,
		proxy: {
			'/api': {
				target: BACKEND,
				changeOrigin: true,
				ws: true,
				configure: devProxy
			},
			// /s carries SSE / streaming responses. Disable buffering so events
			// flush to the client as they arrive instead of being held back.
			'/s': {
				target: BACKEND,
				changeOrigin: true,
				ws: true,
				configure: devProxy
			}
		}
	}
});
