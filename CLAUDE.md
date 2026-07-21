# mcpelevator — self-hosted control plane that elevates stdio MCP servers into authenticated HTTP endpoints

One container runs stdio (or already-remote) MCP servers and exposes each as a remote
Streamable-HTTP endpoint (`/s/<slug>/mcp`) with a SvelteKit UI, process supervision, and auth.

## Commands

Run backend commands from `backend/`, frontend commands from `frontend/`. The `Makefile`
wraps the common ones.

- Backend dev (autoreload, http://127.0.0.1:8080): `cd backend && uv run uvicorn app.main:app --reload --port 8080` — `make dev-backend`
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
  system is idempotent and survives restarts. It also owns **idle quiescence**: an enabled server
  with no proxy traffic inside its idle window (per-server `idle_timeout_s`, else the
  `idle_timeout_s` runtime setting; 0 = off) is stopped into an `idle` state, and the proxy wakes
  it on the next `/s` request, holding the request until readiness (ADR-0002).
- **One bridge process per enabled server** (`bridge/`, `runners/`): each runs its own uvicorn on
  a loopback port hosting a FastMCP proxy of the stdio command (or an upstream HTTP/SSE URL),
  fault-isolated with a real PID and logs. When the server's `rest_openapi` exposure is on, the
  same bridge also serves each tool as plain REST (`/rest/<tool>` + a generated
  `/rest/openapi.json`), reached through the same `/s/<slug>/` proxy path and auth. A server's
  `disabled_tools` (names) installs a FastMCP middleware in the bridge that drops those tools from
  `tools/list` and refuses them on call, so the filter covers every surface at once (MCP, REST, and
  the group hub, which all resolve tools through this proxy); it's part of `config_hash`, so a
  change restarts the bridge.
- **Upstream OAuth** (`auth/oauth_store.py`, `auth/oauth_flow.py`): a `remote` server can
  authenticate to its upstream via OAuth instead of static `env` headers. The interactive
  authorization-code grant (DCR + PKCE) runs in the control plane (`/api/servers/{id}/oauth/authorize`
  → public `/api/oauth/callback`, anchored on the OAuth `state`) using the MCP SDK's
  `OAuthClientProvider`. Client identity is provider-adaptive: a static client id/secret when
  set, else the instance's CIMD client-metadata document (public
  `/api/oauth/client-metadata.json`, offered only from an https base) where the provider
  advertises URL-based client ids, else DCR; tokens land in a `0600` file store
  (`<data_dir>/oauth/<id>.json`) shared
  with the bridge, which reads them and auto-refreshes. Tokens live off the DB, so authenticating
  never re-hashes the row or bounces the bridge.
- **Runners** (`runners/`): `npx`, `uvx`, `command`, `remote` (proxy an already-remote MCP URL),
  and `docker` (image-packaged servers — opt-in + root-equivalent behind the `docker_runner`
  setting). Each runner is a pure `Server -> ProcessSpec` builder; `docker` stores the canonical
  image+container-args+env shape and synthesizes a hardened `docker run` (the bridge scrubs the
  child env for docker units so a `-e KEY` passthrough can't reach the control plane's secrets).
- **Group registry** (`groups/`): the `groups` runtime setting is the single source of truth
  mapping a group name to its members — either `"*"` (every registered server, present and
  future) or an ordered list of server ids. Each group is served at `/g/<name>/mcp`: a
  control-plane-hosted FastMCP mounting a proxy per running member, tools namespaced by slug,
  rebuilt-and-swapped per group after each reconcile via the supervisor's `on_converged` hook
  (the hub owns each sub-app's lifespan; Starlette doesn't run mounted lifespans). There is no
  special-case name — `all` is just a conventional entry with members `"*"`. The registry is
  validated at write time and again at startup (`registry.validate_at_startup` fails the boot
  on an unknown member id, naming the offending group + server); an unknown group name 404s at
  request time and an empty group serves a valid tool-less bundle. Auth runs through the same
  `enforce()` with a synthetic `group:<name>` pseudo-server: bearer requires a matching
  `group:<name>`-scoped (or `all`) token, and members stricter than the default provider are
  excluded when the default is `none`. There is no `/s/all` — single servers live only under
  `/s/<slug>`, groups only under `/g/<name>`, so `all` is now an ordinary server slug.
- **Auth** (`auth/`): two independent layers per request — a Host/Origin allowlist middleware
  (DNS-rebinding defense) plus pluggable per-server bearer auth on `/s` and control-plane bearer
  auth that gates the sensitive `/api` routers only when enforcement is on (default `auto`: when
  the box is exposed off-host); `/api/health*` and `/api/auth/status` stay public. See README
  "Security" for the full model.
- **Multi-user control plane** (`auth/principal.py`, `auth/policy.py`, `api/users.py`): WHO is
  resolved in exactly one place (`principal.resolve` — enforcement off ⇒ synthetic local admin,
  `MCPE_ADMIN_TOKEN` ⇒ env admin, a user-less control token ⇒ legacy admin, a user-bound one ⇒
  that user's role/flags) and WHAT they may do lives entirely in `policy` (server/token
  visibility, the local-runner permission, member token-scope limits) — routers call those
  predicates, never re-derive rules. Users hold no passwords: credentials are `control` tokens
  bound via `Token.user_id`; `Server.owner_id` (NULL = admin-owned, the upgrade default) drives
  visibility, and non-visible resources 404 like nonexistent ones. Ownership is identity, not
  launch config — it stays out of `config_hash`, so reassigning never bounces a bridge. This is
  authorization, not isolation: local runners execute as the process user (README "Security",
  trust caveat).
- **Catalog** (`catalog/`): backend proxies public MCP directories (official registry, Glama) into
  reviewable launch specs; the SPA stays same-origin.
- **Frontend** (`frontend/src/`): SvelteKit (Svelte 5) SPA, `adapter-static`, no SSR — rendered
  entirely in the browser, served by the backend catch-all.

## Conventions

- Route order matters: `/api` and `/s` routers are registered before the SPA catch-all mount so
  they win over the client-side router (`backend/app/main.py`). Keep new API/proxy routes above it.
- Svelte runes mode is forced everywhere via `compilerOptions.runes` (`frontend/svelte.config.js`)
  — the codebase is Svelte 5; write runes, not legacy reactive syntax.
- Backend dependencies are lockfile-driven: change `pyproject.toml`, then run `uv lock` so the
  committed `uv.lock` stays in sync for reproducible installs (the Docker build prefers `--frozen`,
  falling back to a resolve).
- Config is env vars prefixed `MCPE_` (`backend/app/config.py`; table in README "Configuration").
- Version derives from the GitHub release tag — never hardcode a version string. The CI release
  workflow (`.github/workflows/docker-image.yml`) passes the tag as `APP_VERSION`; the `Dockerfile`
  sets it as `ENV MCPE_VERSION`; `app.__version__` (`backend/app/__init__.py`) reads `MCPE_VERSION`
  first, then the adjacent `pyproject.toml` (source tree), then installed metadata. `backend/pyproject.toml` /
  `frontend/package.json` versions are source metadata that only track the dev line (the tag wins
  at deploy); bump them when cutting a release and run `uv lock`.
- Add a catalog directory as a plugin: one `Source` module + one line in the source registry
  (`backend/app/catalog/README.md`).
- UI screenshots in `docs/screenshots/` (shown in the README) are generated by shot-scraper, not
  hand-edited. When you change the UI, the Screenshots workflow (`.github/workflows/screenshots.yml`)
  refreshes them on a push to `main` or manual dispatch, committing the new PNGs in place. If you add
  or remove a screen, update `shots.yml` (the shot list, source of truth) and the README gallery to
  match. Regenerate locally: `make build`, then `bash scripts/screenshots-serve.sh`, then
  `shot-scraper multi shots.yml --retina`.

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `pacnpal/mcpelevator`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix` labels. See `docs/agents/triage-labels.md`.

### Domain docs

This repo uses a single-context layout with `CONTEXT.md` at the root and ADRs under `docs/adr/`. See `docs/agents/domain.md`.
