# mcpelevator

**Elevate stdio MCP servers into authenticated HTTP endpoints. Self-hosted, in one container.**

Most [MCP](https://modelcontextprotocol.io) servers ship as **stdio** programs (`npx -y …`, `uvx …`, a command, a docker image). Stdio only works when the client can spawn the process locally, which **phones and most "any device" setups can't do**. mcpelevator runs those servers for you and exposes each one as a remote **Streamable HTTP** endpoint (the transport Claude mobile, Flutter clients, etc. connect to), plus an optional REST/OpenAPI surface. Add a server, press start, copy the URL into your client.

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

The image is batteries-included (Node/npx + Python/uv preinstalled), so `npx`/`uvx` servers run with no extra setup. Data (SQLite + package caches) persists in the `mcpe-data` volume. By default the port is published to host loopback only. See **Security**.

## Quickstart (local dev)

```bash
# backend (control plane) on http://127.0.0.1:8080
cd backend && uv sync && uv run uvicorn app.main:app --reload

# frontend (HMR) on http://localhost:5173, proxies /api and /s to :8080
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
| `MCPE_TRUSTED_PROXIES` | _(none)_ | CIDRs whose peer IPs count as loopback for the Host guard (reverse proxy / Docker bridge gateway) |
| `MCPE_ADMIN_TOKEN` | _(none)_ | Break-glass control-plane token, always accepted on `/api` |
| `MCPE_DATA_DIR` | `./data` | SQLite + caches |
| `MCPE_FRONTEND_DIR` | `../frontend/build` | Built SPA to serve |
| `MCPE_PORT_RANGE_START` / `_END` | `49200` / `49400` | Loopback ports for bridge processes |
| `MCPE_MAX_RUNNING` | `50` | Cap on concurrent running servers |
| `MCPE_START_TIMEOUT_S` | `120` | Readiness timeout (covers npx/uvx cold start) |

## Security

> **Exposing this to Claude?** See [docs/claude-web-exposure.md](docs/claude-web-exposure.md)
> for the secure paths — and why claude.ai **web** and **mobile** (OAuth-only) differ
> from Claude Code / Desktop (which can use mcpelevator's bearer auth directly).

Two independent layers guard the system, and a request must pass both.

**Host/Origin allowlist** (DNS-rebinding defense), enforced on every request in every mode. A loopback `Host` is trusted only when the request's **peer** actually connects from loopback, so an off-host bind can't spoof `Host: localhost`. `expose` mode adds the hosts you allowlist, and the host in `MCPE_PUBLIC_BASE_URL` is always trusted. Behind a local reverse proxy or Docker's bridge gateway (where the peer is the forwarder, not the real client), set `MCPE_TRUSTED_PROXIES` (CIDRs) to trust it. The default `docker-compose.yml` does this for the bridge range, which is safe only with a loopback-published port.

**Per-request bearer auth**, on both planes:

- The **proxy data plane** (`/s`) uses a pluggable per-server auth provider. v1 ships `none` and `bearer` (SHA-256-hashed tokens); a server set to `bearer` needs a token in `Authorization: Bearer <token>`. A token authorizes every bearer-protected server by default, or you can scope it to a single server when you create it.
- The **control plane** (`/api`) requires an admin token with the `control` scope. Enforcement follows the `control_plane_auth` setting: `auto` (the default) requires it when `bind_mode=expose` or `MCPE_PUBLIC_BASE_URL` is set (either way the instance is reachable off-host), so a plain local install stays zero-config; `always` requires it even on loopback. `/api/health` and `/api/auth/status` stay public.

When control-plane auth is enforced, the SPA shows a login screen. The admin token is printed once to the container logs on first boot (look for "control-plane auth is ON"), and the Settings page can generate one (which logs you in immediately). To switch to `expose` or `always` from the UI you have to generate an admin token first, so you can't lock yourself out.

`MCPE_ADMIN_TOKEN` is a break-glass credential: when set, it's always accepted on `/api`. Use it to recover a lost token, or for CI and automation. A minted token is shown only once (only its hash is stored), so if you lose it and haven't set `MCPE_ADMIN_TOKEN`, set that var and restart to get back in, then generate a fresh token.

The `docker` runner (launch MCP servers that are Docker images) is **opt-in and root-equivalent**, milestone **M7**.

## Project layout

```
backend/app/   FastAPI control plane, supervisor, bridge host, runners, auth, proxy
frontend/      SvelteKit (Svelte 5) SPA, adapter-static
Dockerfile     multi-stage: build SPA → python+node+uv runtime
```

## Status / roadmap

**Working today:** add a server (guided form, or paste an `mcpServers` config), supervise it, and use it over Streamable HTTP from any MCP client. Per-server detail with **live log streaming**, config, and discovered tools; edit / delete / start / stop. **Per-client copy** menu (Claude Code, Codex, `mcpServers` / VS Code JSON, raw URLs). Runners: `npx`, `uvx`, `command`. **Auth**: bearer tokens for `/s` (scope each to all servers or one), control-plane bearer auth for `/api` with an admin login, and a Host/Origin allowlist (Settings) for safe exposure.

**Planned:** REST/OpenAPI surface per server · `docker` runner · a server catalog · polish.

## License

[MIT](LICENSE) © pacnpal
