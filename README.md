# mcpelevator

**Elevate stdio MCP servers into authenticated HTTP endpoints — self-hosted, in one container.**

Most [MCP](https://modelcontextprotocol.io) servers ship as **stdio** programs (`npx -y …`, `uvx …`, a command, a docker image). Stdio only works when the client can spawn the process locally — which **phones and most "any device" setups can't do**. mcpelevator runs those servers for you and exposes each one as a remote **Streamable HTTP** endpoint (the transport Claude mobile, Flutter clients, etc. connect to), plus an optional REST/OpenAPI surface. Add a server, press start, copy the URL into your client.

The protocol bridging is done by [FastMCP](https://gofastmcp.com); mcpelevator is the **control plane**: a clean UI, process supervision, security, and onboarding around it.

## How it works

```
[Claude mobile / any MCP client] ──Streamable HTTP──┐
                                                    ▼
  FastAPI ─ /            SvelteKit SPA              (one container, one port)
          ─ /api/*       control plane (SSOT in SQLite)
          ─ /s/<slug>/mcp   reverse-proxy ─┐  auth + Host/Origin enforced here
                                           ▼
  per enabled server: 1 supervised bridge process (own uvicorn on a loopback port)
      FastMCP.as_proxy(stdio server) → Streamable HTTP   ← fault-isolated, real PID/logs
```

A reconciler converges running processes to the desired state in SQLite (Kubernetes-style), so the system is idempotent and survives restarts.

## Quickstart (Docker)

```bash
docker compose up --build
# open http://127.0.0.1:8080
```

The image is batteries-included (Node/npx + Python/uv preinstalled), so `npx`/`uvx` servers run with no extra setup. Data (SQLite + package caches) persists in the `mcpe-data` volume. By default the port is published to host loopback only — see **Security**.

## Quickstart (local dev)

```bash
# backend (control plane) — http://127.0.0.1:8080
cd backend && uv sync && uv run uvicorn app.main:app --reload

# frontend (HMR) — http://localhost:5173, proxies /api and /s to :8080
cd frontend && npm install && npm run dev
```

Or use the `Makefile`: `make dev-backend`, `make dev-frontend`, `make build`, `make test`, `make docker`.

## Adding a server

Via the API (the UI add-flow wraps this):

```bash
curl -X POST http://127.0.0.1:8080/api/servers -H 'content-type: application/json' -d '{
  "name": "Memory", "runner": "npx", "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-memory"], "enabled": true
}'
```

Then point any MCP client at `http://127.0.0.1:8080/s/memory/mcp`.

## Configuration (env vars, prefix `MCPE_`)

| Var | Default | Meaning |
|---|---|---|
| `MCPE_HOST` | `127.0.0.1` | Control-plane bind (Docker sets `0.0.0.0`) |
| `MCPE_PORT` | `8080` | Control-plane port |
| `MCPE_PUBLIC_BASE_URL` | _(derived)_ | Absolute URL clients use (set behind a tunnel) |
| `MCPE_DATA_DIR` | `./data` | SQLite + caches |
| `MCPE_FRONTEND_DIR` | `../frontend/build` | Built SPA to serve |
| `MCPE_PORT_RANGE_START` / `_END` | `49200` / `49400` | Loopback ports for bridge processes |
| `MCPE_MAX_RUNNING` | `50` | Cap on concurrent running servers |
| `MCPE_START_TIMEOUT_S` | `120` | Readiness timeout (covers npx/uvx cold start) |

## Security

- **Local-first by default**: compose publishes to `127.0.0.1` only. To reach it from your phone, front it with a tunnel (Tailscale / Cloudflare Tunnel) or expose the port deliberately — **after** enabling auth.
- Auth is a **pluggable seam** (single enforcement chokepoint): v1 ships `none` + `bearer` (SHA-256-hashed tokens). The chokepoint also enforces a Host/Origin allowlist (DNS-rebinding defense) on every request — loopback always, plus the hosts you allowlist for `expose`.
- The **control plane** (`/api`) is **not** per-request authenticated in v1 — it's guarded only by the same Host/Origin allowlist. That allowlist stops browser DNS-rebinding, but it is **not** a substitute for auth against a direct attacker (who can set any `Host`). So keep the default `127.0.0.1` publish and front remote access with an authenticating tunnel/proxy (Tailscale / Cloudflare Tunnel) rather than binding `/api` off-host directly. Per-request control-plane auth is a tracked follow-up.
- The `docker` runner (launch MCP servers that are Docker images) is **opt-in and root-equivalent** — milestone **M7**.

## Project layout

```
backend/app/   FastAPI control plane, supervisor, bridge host, runners, auth, proxy
frontend/      SvelteKit (Svelte 5) SPA, adapter-static
Dockerfile     multi-stage: build SPA → python+node+uv runtime
```

## Status / roadmap

**Working today:** add a server (guided form, or paste an `mcpServers` config) → supervise it → use it over Streamable HTTP from any MCP client. Per-server detail with **live log streaming**, config, and discovered tools; edit / delete / start / stop. **Per-client copy** menu (Claude Code, Codex, `mcpServers` / VS Code JSON, raw URLs). Runners: `npx`, `uvx`, `command`. **Auth**: bearer tokens (global, opted in per server) + a Host/Origin allowlist (Settings) for safe exposure.

**Planned:** REST/OpenAPI surface per server · `docker` runner · a server catalog · polish.

## License

[MIT](LICENSE) © pacnpal
