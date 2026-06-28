# mcpelevator

**Elevate stdio MCP servers into authenticated HTTP endpoints. Self-hosted, in one container.**

Most [MCP](https://modelcontextprotocol.io) servers ship as **stdio** programs (`npx -y …`, `uvx …`, a command, a docker image). Stdio only works when the client can spawn the process locally, which **phones and most "any device" setups can't do**. mcpelevator runs those servers for you and exposes each one as a remote **Streamable HTTP** endpoint (the transport Claude mobile, Flutter clients, etc. connect to), plus an optional REST/OpenAPI surface. Add a server, press start, copy the URL into your client.

Already-remote servers work too: point mcpelevator at an existing Streamable-HTTP/SSE MCP URL (the `remote` runner) and it proxies that upstream behind the same auth, supervision, and per-client copy menu as a local one — handy for putting bearer auth in front of a remote server, or giving every client one consistent endpoint.

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
      FastMCP proxy(stdio command — or a remote HTTP/SSE URL) → Streamable HTTP   ← fault-isolated, real PID/logs
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

**Already remote?** Use the `remote` runner to proxy an existing Streamable-HTTP/SSE MCP URL — no local process. The launch spec reuses the same fields: `command` is the upstream URL, `args[0]` is the transport (`streamable-http` or `sse`), and `env` is the upstream HTTP headers.

```bash
curl -X POST http://127.0.0.1:8080/api/servers -H 'content-type: application/json' -d '{
  "name": "Remote MCP", "runner": "remote",
  "command": "https://example.com/mcp", "args": ["streamable-http"],
  "env": {"Authorization": "Bearer <upstream-token>"}, "enabled": true
}'
```

Pasting an `mcpServers` config that contains remote entries (`url` / `httpUrl` / `type` / `transport`) imports them as `remote` servers too — they're no longer skipped.

## Install from a registry (catalog)

Don't know the package name? **Browse** finds servers for you. The catalog searches public
MCP directories and resolves a chosen server into a launch spec you review and install — no
hand-typing `npx -y …`.

- **MCP Registry** (`registry.modelcontextprotocol.io`) — the official directory. Its
  servers carry structured packages, so npm → `npx` and pypi → `uvx` are derived
  automatically and pinned to the listed version: **one-click install**.
- **Glama** (`glama.ai`) — a larger, curated directory for **discovery**. It publishes no
  launch command, so installs open the review form pre-filled with the name + required
  env-var keys + a repo link for you to complete.

**Filter by type** — narrow the browse list to one or more package/registry types (`npm`,
`pypi`, `oci`, `nuget`, `mcpb`, `remote`) with the type chips. A server that publishes a
remote (HTTP/SSE) endpoint is installable as a proxied **`remote`** server: install carries
the endpoint's declared headers into the review form (required ones flagged) so you can fill
in upstream auth before starting.

Open **Browse** in the header (or `/catalog`). The backend proxies the directories
(`GET /api/catalog/servers`, `GET /api/catalog/server`) so the SPA stays same-origin;
installing posts the reviewed draft to `POST /api/servers` tagged `source=catalog:<id>`.

Adding another directory is a small plugin: one `Source` module + one line in the source
registry — see [`backend/app/catalog/README.md`](backend/app/catalog/README.md).

## Configuration (env vars, prefix `MCPE_`)

| Var | Default | Meaning |
|---|---|---|
| `MCPE_HOST` | `127.0.0.1` | Control-plane bind (Docker sets `0.0.0.0`) |
| `MCPE_PORT` | `8080` | Control-plane port |
| `MCPE_PUBLIC_BASE_URL` | _(derived)_ | Absolute URL clients use (set behind a tunnel) |
| `MCPE_TRUSTED_PROXIES` | _(none)_ | CIDRs whose peer IPs count as loopback for the Host guard (reverse proxy / Docker bridge gateway) |
| `MCPE_TRUST_DOCKER_HOST` | `false` | Auto-detect the container's default gateway (the Docker host) and trust it for the Host guard, without hardcoding the gateway CIDR. Opt-in: safe only with a **loopback**-published port (`-p 127.0.0.1:8080:8080`) — under userland-proxy a `0.0.0.0` publish presents the gateway as the peer too, so enabling it there trusts LAN traffic. Loopback allowance only; the bearer-token gate still applies |
| `MCPE_ALLOWED_HOSTS` | _(none)_ | Comma-separated extra hostnames the Host/Origin guard always trusts (like the `MCPE_PUBLIC_BASE_URL` host, for additional origins). Setting it turns control-plane auth on under `auto` (the box is reachable off-host via that hostname) |
| `MCPE_ADMIN_TOKEN` | _(none)_ | Break-glass control-plane token, always accepted on `/api` |
| `MCPE_MINT_ADMIN_TOKEN` | `false` | Force-mint a fresh admin token on boot and print it (recovery for a lost token); unset after grabbing it |
| `MCPE_ALLOW_PRIVATE_LAN` | `false` | First-boot seed for the LAN-access setting (headless bootstrap); see **Security** |
| `MCPE_DATA_DIR` | `./data` | SQLite + caches |
| `MCPE_FRONTEND_DIR` | `../frontend/build` | Built SPA to serve |
| `MCPE_PORT_RANGE_START` / `_END` | `49200` / `49400` | Loopback ports for bridge processes |
| `MCPE_MAX_RUNNING` | `50` | Cap on concurrent running servers |
| `MCPE_START_TIMEOUT_S` | `120` | Readiness timeout (covers npx/uvx cold start) |

## Security

> **Want to expose this over the internet — e.g. to reach it from Claude?** See
> [docs/claude-web-exposure.md](docs/claude-web-exposure.md)
> for two concrete, secure recipes: **Path A** (claude.ai **web/mobile**) —
> a Cloudflare Tunnel plus a Cloudflare Access self-hosted app with **Managed OAuth**,
> since web/mobile and Desktop's account-UI **remote connectors** are OAuth-only and
> can't send a bearer; and **Path B** (Claude **Code** / **locally-configured**
> Desktop) — a public HTTPS tunnel plus mcpelevator's built-in **bearer** auth.
> The guide has the exact `cloudflared`/Access steps, `curl` checks, and the
> connector caveats to test before relying on web/mobile.

Two independent layers guard the system, and a request must pass both.

**Host/Origin allowlist** (DNS-rebinding defense), enforced on every request in every mode. A loopback `Host` is trusted only when the request's **peer** actually connects from loopback, so an off-host bind can't spoof `Host: localhost`. `expose` mode adds the hosts you allowlist, and the host in `MCPE_PUBLIC_BASE_URL` is always trusted. Behind a local reverse proxy or Docker's bridge gateway (where the peer is the forwarder, not the real client), set `MCPE_TRUSTED_PROXIES` (CIDRs) to trust it. The default `docker-compose.yml` does this for the bridge range, which is safe only with a loopback-published port.

**Local network (LAN) access** — for a self-hosted box (Unraid, a NAS, a home server) you want to reach from your phone or laptop on the same network, turn on **Allow access from devices on your local network** (the `allow_private_lan` setting — Settings page or `PATCH /api/settings`). It lets a request whose `Host` is a **private-IP literal** (e.g. `http://192.168.1.50:8080`) through the guard **when the connecting peer is itself on a private network** — no per-host allowlisting, and no DNS-rebinding hole, because a rebinding attack delivers the attacker's *domain* in the `Host` header, never a bare private-IP literal. Only IP literals qualify; a hostname that resolves to a LAN address is still rejected. Bind the socket off-host for this: the Docker image already binds `0.0.0.0`, but a **source install** must launch uvicorn with `--host 0.0.0.0` (`uvicorn app.main:app --host 0.0.0.0`) — `MCPE_HOST` only feeds derived URLs there, it doesn't move the dev-server bind. Because the instance is now reachable off-host, enabling LAN access turns on control-plane auth under `auto` (so `/api` requires an admin token). It is **off by default**.

*Getting in the first time* (the token-vs-access chicken-and-egg): on a fresh install `/api` is open from **loopback** with no token, so the simplest path is to mint the admin token on the box itself — or over an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 you@box`, then open `http://localhost:8080`) — which logs that browser in, and *then* turn LAN access on. For a **headless** box with no loopback browser, set `MCPE_ALLOW_PRIVATE_LAN=true` (and optionally `MCPE_ADMIN_TOKEN`): it seeds the setting on first boot, and because that turns control-plane auth on, the startup bootstrap mints an admin token and **prints it once to the container logs** (`docker compose logs`, or Unraid's log viewer) for you to log in with from the LAN. The env var only seeds the initial value — the Settings toggle is authoritative afterwards.

**Per-request bearer auth**, on both planes:

- The **proxy data plane** (`/s`) uses a pluggable per-server auth provider. v1 ships `none` and `bearer` (SHA-256-hashed tokens); a server set to `bearer` needs a token in `Authorization: Bearer <token>`. A token authorizes every bearer-protected server by default, or you can scope it to a single server when you create it.
- The **control plane** (`/api`) requires an admin token with the `control` scope. Enforcement follows the `control_plane_auth` setting: `auto` (the default) requires it when `bind_mode=expose` or `MCPE_PUBLIC_BASE_URL` is set (either way the instance is reachable off-host), so a plain local install stays zero-config; `always` requires it even on loopback. `/api/health` (control-plane liveness), `/api/health/{slug}` and `/api/health/summary` (per-server readiness, for load balancers), and `/api/auth/status` stay public.

When control-plane auth is enforced, the SPA shows a login screen. The admin token is printed once to the container logs on first boot (look for "control-plane auth is ON"), and the Settings page can generate one (which logs you in immediately). To switch to `expose` or `always` from the UI you have to generate an admin token first, so you can't lock yourself out.

`MCPE_ADMIN_TOKEN` is a break-glass credential: when set, it's always accepted on `/api`. Use it to recover a lost token, or for CI and automation. A minted token is shown only once (only its hash is stored), so if you lose it and haven't set `MCPE_ADMIN_TOKEN`, set that var and restart to get back in, then generate a fresh token. Alternatively, set `MCPE_MINT_ADMIN_TOKEN=true` and restart: the bootstrap mints a fresh control token and prints it to the logs (existing tokens keep working) — unset the var afterwards so it doesn't mint a new one on every restart.

The `docker` runner (launch MCP servers that are Docker images) is **opt-in and root-equivalent**, milestone **M7**.

## Project layout

```
backend/app/   FastAPI control plane, supervisor, bridge host, runners, auth, proxy, catalog
frontend/      SvelteKit (Svelte 5) SPA, adapter-static
Dockerfile     multi-stage: build SPA → python+node+uv runtime
```

## Status / roadmap

**Working today:** add a server (guided form, paste an `mcpServers` config — stdio or remote, or **browse a registry** and install with one review), supervise it, and use it over Streamable HTTP from any MCP client. Per-server detail with **live log streaming**, config, and discovered tools; edit / clone / delete / start / stop. **Clone** a server to spin up a like-configured copy in one click, and **rename a server's slug** to re-point its `/s/<slug>/` URLs (clients pointed at the old slug need re-pointing). **Per-client copy** menu grouped by ecosystem — Claude Code, Claude Desktop (via `mcp-remote`), Claude web / mobile connectors, Codex, ChatGPT connectors, Gemini CLI, VS Code, generic `mcpServers`, and raw URLs. Runners: `npx`, `uvx`, `command`, and `remote` (proxy an already-remote Streamable-HTTP/SSE MCP URL). **Catalog** browse with a **by-type filter** (npm/pypi/oci/nuget/mcpb/remote) and one-review install, including remote endpoints. **Auth**: bearer tokens for `/s` (scope each to all servers or one), control-plane bearer auth for `/api` with an admin login, a Host/Origin allowlist (Settings) for safe exposure, and an opt-in LAN-access toggle for self-hosted boxes.

**Planned:** REST/OpenAPI surface per server · `docker` runner (would unlock OCI catalog installs) · more catalog directories · polish.

## License

[MIT](LICENSE) © pacnpal
