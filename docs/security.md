# Security posture & threat model

This document is the reference threat model for mcpelevator: what it protects,
who the attackers are, where the trust boundaries sit, and how findings should be
triaged by severity. For hands-on hardening steps see the **Security** section of
the [README](../README.md) and, for internet exposure,
[docs/claude-web-exposure.md](claude-web-exposure.md).

## 1. Overview

mcpelevator is a self-hosted FastAPI + SvelteKit control plane that turns stdio or
already-remote MCP servers into remote Streamable-HTTP endpoints. The backend serves
a static SPA, control-plane APIs under `/api`, and a data-plane reverse proxy under
`/s/<slug>/...`. Desired state, settings, and token hashes are stored in SQLite; a
supervisor starts one bridge subprocess per enabled server, each bound to a loopback
port and wrapping a local command (`npx`, `uvx`, arbitrary command) or a remote
HTTP/SSE upstream.

The primary assets are: control-plane authority to create/enable servers and tokens,
per-server data-plane bearer tokens, upstream header/env secrets, the SQLite database,
host/container filesystem and network access available to child MCP processes, and the
integrity of Host/Origin/DNS-rebinding defenses. By design, an authenticated admin can
obtain local command execution and SSRF-like network access by adding a command or
remote server. Therefore, "RCE" or "SSRF" is critical only when an unauthenticated,
off-host, CSRF/DNS-rebinding, or improperly scoped caller reaches those capabilities.

The security posture changes by deployment mode: loopback-only defaults to zero-config
`/api`, LAN access can be enabled via `allow_private_lan` and forces admin auth under
`auto`, and public/tunnel deployments should set `MCPE_PUBLIC_BASE_URL`,
`bind_mode=expose`, allowed hosts, and per-server bearer or an external OAuth/access
proxy.

## 2. Threat model, trust boundaries and assumptions

**Attacker-controlled inputs:** HTTP requests to `/api`, `/s`, and static assets;
`Host`, `Origin`, query/path/body, and bearer tokens; MCP client traffic proxied to
bridges; catalog search/cursor parameters; data-plane long-lived SSE connections;
untrusted output from child MCP processes shown in logs.

**Operator-controlled inputs:** runtime settings (`bind_mode`, `allowed_hosts`,
`default_auth_provider`, `control_plane_auth`, `allow_private_lan`), server launch specs
(`runner`, `command`, `args`, `env`, `cwd`, `auth_provider`, `enabled`), `mcpServers`
imports, token creation/revocation, Docker/env configuration (`MCPE_ADMIN_TOKEN`,
`MCPE_TRUSTED_PROXIES`, `MCPE_PUBLIC_BASE_URL`, etc.), and deployment topology.
Operator/admin inputs are privileged: they may intentionally execute commands or proxy
internal URLs.

**External-but-untrusted inputs reviewed by an admin:** official registry and Glama
catalog responses. The backend fetches only fixed registry hosts, then maps
packages/remotes into install drafts that the admin reviews before `POST /api/servers`.

**Developer/CI-controlled inputs:** tests, build scripts, Dockerfile and GitHub Actions.
These affect supply-chain integrity, not runtime request authorization.

**Trust assumptions:** There is no multi-user/RBAC boundary. A control-scope admin token
is root-equivalent for the application. Data-plane tokens are shared secrets; an `all`
token can use every bearer-protected server, while a server-id token must stay scoped to
that server. Child MCP packages and remote upstreams may be malicious; mcpelevator gives
them the container permissions and network available at runtime. Logs and environment
variables are sensitive. A reverse proxy is trusted only if its real peer IP is in
`MCPE_TRUSTED_PROXIES` or `MCPE_TRUST_DOCKER_HOST` is deliberately enabled; the code does
not trust `X-Forwarded-For`.

## 3. Attack surface, mitigations and attacker stories

**Control plane (`/api`).** Sensitive routers in `backend/app/main.py` depend on
`require_control_plane` from `backend/app/auth/control_plane.py`; health and auth-status
remain public but are intentionally coarse. Enforcement is `always`, or `auto` when
`bind_mode=expose`, `MCPE_PUBLIC_BASE_URL`/`MCPE_ALLOWED_HOSTS` declares an off-host
origin, or `allow_private_lan` is enabled. A serious attacker story is an off-host
request creating an enabled `command` server or minting tokens without a control token.
Relevant controls include settings lockout checks (`would_lock_out`), requirement that
the current request already authenticate before enabling auth/exposure, protection
against revoking the last control token while enforced, and idempotent first-boot token
minting.

**Host/Origin and DNS rebinding.** `backend/app/auth/middleware.py` is the edge guard for
both `/api` and `/s`: Host is mandatory, Origin is checked when present, loopback
hostnames are accepted only from loopback or configured trusted proxy peers, and LAN mode
accepts only private-IP literals from private-network peers. It deliberately does not
resolve hostnames to private IPs, preventing classic DNS rebinding with `evil.example`.
Trusted proxies are evaluated from the TCP peer, not `X-Forwarded-For`; trusted forwarders
are excluded from the LAN peer gate. Misconfiguring Docker by publishing `0.0.0.0:8080`
while keeping gateway trust is a high-risk operator error documented in `docker-compose.yml`
and docs.

**Data plane (`/s/<slug>/...`).** `backend/app/proxy/router.py` looks up the slug, calls
the same `enforce()` chokepoint, then streams to the loopback bridge, stripping
hop-by-hop headers and content encoding. Bearer tokens are SHA-256-hashed at rest and
matched by hash lookup rather than plaintext comparison. There is no data-plane
break-glass credential — `MCPE_ADMIN_TOKEN` is accepted only on the control plane
(where it is compared with `secrets.compare_digest`), never as a `/s/<slug>` token. Data
tokens with `scope=all` or the matching
`server.id` are accepted, while `control` tokens are not valid data-plane tokens. Unknown
auth providers fail closed and API schemas restrict provider values. A critical bug would
be resolving `bearer` or `inherit` to `none`, accepting a single-server token for another
server, or allowing `Host: localhost` from a remote peer. A server explicitly set to `none`
is not itself a vulnerability when the operator intentionally relies on loopback, LAN trust,
or an external OAuth/Access gate; it is dangerous if exposed directly to the internet.

**Command execution and supervision.** `backend/app/api/servers.py`,
`backend/app/registry/service.py`, `backend/app/runners/`, `backend/app/supervisor/unit.py`,
and `backend/app/bridge/host.py` turn stored specs into child processes. Local runners pass
argv lists, not shell strings, and the supervisor executes the bridge module with
`asyncio.create_subprocess_exec`. The **docker runner** is opt-in and root-equivalent: it is
disabled by default behind the `docker_runner` setting, enforced at the service layer (a
docker server can't be created-enabled or enabled while it's off) and again in the
supervisor (reconcile refuses to start a docker unit while it's off). The catalog gate is
separate — OCI installs are surfaced non-installable until the runner is enabled. When enabled it stores the canonical image+args+env shape and synthesizes a hardened
`docker run` (`--rm --init --cap-drop ALL --security-opt no-new-privileges --pids-limit` +
a memory cap). Secrets are passed by **name** (`-e KEY`), never `KEY=value`, so a value never
enters mcpelevator's own argv/`ps` (Docker still resolves it into the container config, which
anyone with Docker daemon access can read via `docker inspect` — name-only passing narrows
exposure to daemon-holders, it doesn't hide the value from them); and the bridge gives a
docker child a **minimal env** (only
`PATH/HOME/DOCKER_*` plus the server's declared vars), so a `-e KEY` passthrough can never
reach the control plane's own secrets (`MCPE_ADMIN_TOKEN`, DB creds); a container also can't
set a `DOCKER_*`/reserved env name — nor a **Go proxy var** (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`/…)
— as a container env (rejected at create and stripped at launch): either would retarget or alter
the CLI's own daemon request (a proxy var would reroute the control-plane's daemon API call on a
TCP `DOCKER_HOST`/dind). Those proxy vars are also NOT inherited from the operator's env into the
CLI, so an operator `HTTP_PROXY` (set for npm/uv) can't break the CLI→dind connection. (To proxy a
launched container, use the Docker CLI **config with `proxies`** in the mcpelevator container — a
Docker CLI behavior outside mcpelevator's env handling; avoid it if launched images are untrusted,
since Docker injects those proxy vars into every launched container.) Each container carries an
`mcpelevator.server=<id>` label (Docker assigns the name) — that label is the sole cleanup
handle: the unit reaps its containers by label on stop (fire-and-forget `docker rm -f`, outside
the stop grace) and the supervisor sweeps labelled orphans (scoped to this instance's server
ids) on boot — the residual gap is a container left by a hard control-plane crash with a
wedged daemon. Isolation is a deployment choice (sibling host socket vs. an isolated dind
sidecar via `DOCKER_HOST`); the sibling model hands the host daemon to containers and is
inherently root-equivalent, which is why the runner is opt-in. Imports create disabled
servers by default; catalog installs (including OCI) are reviewable drafts gated by the same
setting. Resource note: `max_running` bounds concurrent server *units*, not containers — the
proxy opens a fresh upstream per client session, so a single docker server can spawn one
memory-capped (`--memory`) container per concurrent session. This is the same fresh-session
model every runner uses (docker just makes each spawn a heavier container); bound it by
requiring `bearer` auth on docker servers so only authorized clients can open sessions, and
by not exposing untrusted images to unauthenticated `/s` traffic. `max_running`, bounded port allocation, process groups, readiness probes, and log
buffers improve robustness. Remaining risks are expected admin power and supply-chain risk:
malicious MCP packages inherit the bridge environment and can access container files/network,
so `MCPE_ADMIN_TOKEN` and upstream secrets should not be left broadly available.

**Remote runner and catalog.** Remote server URLs are validated as `http(s)` with a
hostname and canonical transport, but there is no egress allowlist or metadata-IP block.
That is acceptable for admins but would be critical if unauthenticated users could
create/enable remote servers. Catalog API calls use fixed upstream base URLs, quoted path
segments, 15s timeouts, capped TTL cache, and deleted/unsupported registry entries are
blocked or marked manual. Untrusted registry data should never be auto-executed without the
normal admin review path.

**Frontend and tokens.** The SPA stores the admin token in `localStorage` and sends it as an
Authorization header, so CSRF via cookies is mostly out of scope, but XSS would steal the
admin token. Svelte escaping and no obvious raw HTML reduce XSS risk; logs from untrusted
processes must continue to be rendered as text. There is no rate limiting; high-entropy
tokens make guessing impractical, but exposed deployments should add outer rate limits.

**CI/build.** The Docker image includes Node/npx and uv/uvx by design. GitHub Actions pins
third-party actions, requires semver release tags, emits SBOM/provenance, and runs
Trivy/Hadolint. Review build-context hygiene as well: without a `.dockerignore`, dirty local
`.env`, `.venv`, or `node_modules` content can enter developer-built images if present. These
are supply-chain controls, separate from runtime authorization.

## 4. Criticality calibration

**Critical:** unauthenticated or wrong-scope access to state-changing `/api` endpoints that
can create/enable commands, remote URLs, settings, or tokens; Host/Origin bypass allowing
off-host `Host: localhost` or DNS rebinding to loopback; data-plane auth bypass where
bearer-protected servers accept no token or the wrong server scope; any path from untrusted
registry/catalog data to enabled command execution without admin review.

**High:** public/LAN request incorrectly considered loopback/private because of trusted-proxy
logic; `control_plane_auth=auto` failing to enforce when an off-host origin or LAN mode is
active; leakage of admin/control tokens via API responses, static assets, logs beyond the
documented one-time bootstrap, or XSS; ability to delete/reset the last admin credential while
enforcement remains on; header injection causing remote runner to send attacker-chosen upstream
auth outside admin intent.

**Medium:** denial of service through many long-lived data-plane streams, large request bodies,
repeated server starts, or catalog cache churn; information disclosure from public health
endpoints beyond coarse readiness; untrusted child logs exposing secrets to any admin; weak
validation that causes bridge endpoints beyond `/mcp` to be reachable; lack of rate limiting for
token attempts despite high entropy.

**Low:** misconfiguration footguns already documented (wrong Docker port publishing, leaving
`none` auth on an exposed server); UI lockout or confusing copy URLs; dependency/CI issues that do
not permit runtime auth bypass; token hashes using unsalted SHA-256 for high-entropy generated
tokens, which is acceptable but should not be reused for human passwords.
