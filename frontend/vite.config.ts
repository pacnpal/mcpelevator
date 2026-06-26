import type { IncomingMessage } from 'node:http';
import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, type ProxyOptions } from 'vite';

// Dev server proxies API + streaming traffic to the FastAPI backend so the
// SPA can call same-origin paths (/api, /s) in development exactly as it will
// in production, where the backend serves the built static files.
const BACKEND = 'http://127.0.0.1:8080';

// Strip buffering hints from streamed (SSE) responses so the proxy flushes
// events to the client as they arrive. Typed via ProxyOptions so it stays
// correct regardless of the bundled http-proxy types.
const unbufferStream: ProxyOptions['configure'] = (proxy) => {
	proxy.on('proxyRes', (proxyRes: IncomingMessage) => {
		proxyRes.headers['cache-control'] = 'no-cache, no-transform';
		proxyRes.headers['x-accel-buffering'] = 'no';
	});
};

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		port: 5173,
		proxy: {
			'/api': {
				target: BACKEND,
				changeOrigin: true,
				ws: true
			},
			// /s carries SSE / streaming responses. Disable buffering so events
			// flush to the client as they arrive instead of being held back.
			'/s': {
				target: BACKEND,
				changeOrigin: true,
				ws: true,
				configure: unbufferStream
			}
		}
	}
});
