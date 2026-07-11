# mcpelevator — self-hosted control plane that elevates stdio MCP servers into authenticated HTTP endpoints

One container runs stdio (or already-remote) MCP servers and exposes each as a remote
Streamable-HTTP endpoint (`/s/<slug>/mcp`) with a SvelteKit UI, process supervision, and auth.

## Commands

Run backend commands from `backend/`, frontend commands from `frontend/`. The `Makefile`
wraps the common ones.

- Backend dev (autoreload, http://127.0.0.1:8080): cd backend && uv run uvicorn app.main:app --reload --port 8080 — make dev-backend
- Frontend dev (HMR, http://localhost:5173, proxies /api and /s to :8080): `cd frontend && npm install && npm run dev` — `make dev-frontend`
- Build SPA into `frontend/build`: `cd frontend && npm ci && npm run build` — `make build`
- Backend tests: `cd backend && uv run pytest -q` — `make test`
- Frontend tests: `cd frontend && npm run test` (vitest)
- Frontend typecheck: `cd frontend && npm run check` (svelte-check)
- Refresh the uv lockfile: `cd backend && uv lock` — `make lock`
- Build + run everything in Docker: `docker compose up --build` — `make docker`

## Architecture

One FastAPI process serves three surfaces in a single port (`backend/app/main.py`):
`/api/*` control plane, `/s/<slug>/mcp` reverse proxy, and the built SPA as a catch-all mount.

- **Desired-state reconciliation.** SQLite is the source of truth. A background supervisor task
  (`supervisor/`) converges running processes to the desired state (Kubernetes-style), so the
  system is idempotent and survives restarts.
- **One bridge process per enabled server** (`bridge/`, `runners/`): each runs its own uvicorn on
  a loopback port hosting a FastMCP proxy of the stdio command (or an upstream HTTP/SSE URL),
  fault-isolated with a real PID and logs.
- **Runners** (`runners/`): `npx`, `uvx`, `command`, `remote` (proxy an already-remote MCP URL),
  and `docker` (opt-in, root-equivalent, milestone M7).
- **Auth** (`auth/`): two independent layers per request — a Host/Origin allowlist middleware
  (DNS-rebinding defense) plus pluggable per-server bearer auth on `/s` and a control-plane admin
  token on `/api`. See README "Security" for the full model.
- **Catalog** (`catalog/`): backend proxies public MCP directories (official registry, Glama) into
  reviewable launch specs; the SPA stays same-origin.
- **Frontend** (`frontend/src/`): SvelteKit (Svelte 5) SPA, `adapter-static`, no SSR — rendered
  entirely in the browser, served by the backend catch-all.

## Conventions

- Route order matters: `/api` and `/s` routers are registered before the SPA catch-all mount so
  they win over the client-side router (`backend/app/main.py`). Keep new API/proxy routes above it.
- Svelte runes mode is forced everywhere via `compilerOptions.runes` (`frontend/svelte.config.js`)
  — the codebase is Svelte 5; write runes, not legacy reactive syntax.
- Backend dependencies are lockfile-driven for determinism: change `pyproject.toml`, then run
  `uv lock`; the Dockerfile builds `--frozen`.
- Config is env vars prefixed `MCPE_` (`backend/app/config.py`; table in README "Configuration").
- The image version is stamped from the release tag by CI (`Dockerfile` `APP_VERSION`,
  `.github/workflows/docker-image.yml`) — don't hardcode a version string.
- Add a catalog directory as a plugin: one `Source` module + one line in the source registry
  (`backend/app/catalog/README.md`).
