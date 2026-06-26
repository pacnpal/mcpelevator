# mcpelevator frontend

The SvelteKit SPA for mcpelevator. Svelte 5, TypeScript, adapter-static. In production the FastAPI backend serves this build at `/`. In dev it runs on its own, with Vite proxying `/api` and `/s` to the backend.

For what mcpelevator is and how the pieces fit together, see the [root README](../README.md).

## Develop

```sh
npm install
npm run dev   # http://localhost:5173, proxies /api and /s to the backend on :8080
```

The backend has to be running too. From the repo root use `make dev-backend`, or `cd backend && uv run uvicorn app.main:app --reload`.

## Build

```sh
npm run build     # outputs to build/, which the backend serves in production
npm run preview   # preview the production build locally
```
