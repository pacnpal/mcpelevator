# Deploying mcpelevator on Unraid

mcpelevator runs as a single Docker container on Unraid and turns your server into a
self-hosted hub for MCP servers: add a server in the web UI, press start, and point
MCP clients on your LAN at `http://<unraid-ip>:8080/s/<slug>/mcp`. (Claude **web/mobile**
connectors dial from Anthropic's cloud, not your device — they need the tunnel recipe in
**Exposing beyond your LAN** below; locally-dialing clients like Claude Code, Claude
Desktop, VS Code, and Gemini CLI work with the LAN URL directly.)

The published image is multi-arch (`amd64` + `arm64`) and batteries-included — Node/npx
and Python/uv are preinstalled, so `npx`/`uvx` MCP servers run with zero extra setup.

- **Image:** `ghcr.io/pacnpal/mcpelevator:latest`
- **Template:** [`mcpelevator.xml` in pacnpal/unraid-templates](https://github.com/pacnpal/unraid-templates/blob/main/mcpelevator.xml)

## Install

### Option A — Community Applications

Search for **mcpelevator** in the **Apps** tab and click **Install**. The template
defaults are the recommended setup; review them against the table below and hit
**Apply**.

### Option B — manual template (before/without CA listing)

Unraid 6.10 removed the old "Template repositories" field, so place the XML on the
flash drive yourself:

1. From a terminal on the box (SSH or the web terminal), download the template into
   dockerMan's user-templates folder:

   ```bash
   curl -fL -o /boot/config/plugins/dockerMan/templates-user/my-mcpelevator.xml \
     https://raw.githubusercontent.com/pacnpal/unraid-templates/main/mcpelevator.xml
   ```

2. Go to **Docker → Add Container** and pick **mcpelevator** from the **Template**
   dropdown (under *User templates*).
3. Review the settings against the table below and **Apply**.

## Recommended settings (what the template does and why)

| Setting | Template default | Why |
|---|---|---|
| Network type | **Host** | mcpelevator gates LAN access by the *real* client IP. With bridge networking + a published port, Docker masquerades every client to the bridge gateway IP, so the LAN gate can't tell your phone from the internet. Host networking preserves real peer IPs, so the gate works as designed. Linux-only, which Unraid is. |
| WebUI Port (`MCPE_PORT`) | `8080` | Under host networking there is no port *mapping* — the container binds this port on the host directly. Change it here if 8080 is already taken (it also updates the WebUI link). |
| Appdata (`/data`) | `/mnt/user/appdata/mcpelevator` | SQLite database (servers, tokens, settings) plus the npm/uv package caches, so installed MCP servers survive restarts and cold-start fast. |
| Allow LAN Access (`MCPE_ALLOW_PRIVATE_LAN`) | `true` | Unraid is headless — there's no loopback browser on the box to click the Settings toggle. This seeds the **allow LAN access** setting on first boot so you can reach the UI from another device at `http://<unraid-ip>:8080`. It only seeds the *first* boot; afterwards the toggle in Settings is authoritative. |

Turning LAN access on automatically turns **control-plane auth** on, so `/api` (and the
web UI) require an admin token — see the next section.

## First login: grab the admin token from the logs

Because LAN access enables control-plane auth, mcpelevator **mints an admin token on
first start and prints it once to the container logs**. It is stored only as a hash, so
this is your one chance to copy it:

1. Start the container.
2. Open the container **log** (click the mcpelevator icon → **Logs**).
3. Look for the block:

   ```
   ================================================================
     mcpelevator control-plane auth is ON.
     Admin token (shown once, store it now):  <token>
   ================================================================
   ```

4. Open `http://<unraid-ip>:8080`, paste the token into the login screen, and store it
   in your password manager.

**Lost the token?** Two recovery hatches, both via container env vars (edit the
container, add the variable, restart):

- `MCPE_ADMIN_TOKEN=<your-secret>` — a break-glass token that is always accepted on
  `/api`. Set it, log in, generate a fresh token in Settings, then remove the var.
- `MCPE_MINT_ADMIN_TOKEN=true` — mints a *new* token on boot and prints it to the logs
  (existing tokens keep working). Unset it after grabbing the token, or you'll mint
  another one on every restart.

## Using it

1. Open the web UI, **Add server** (guided form, paste an `mcpServers` JSON config, or
   **Browse** a registry and one-click install).
2. Start it, then use the per-client **copy menu** on the server page — it has ready-made
   snippets for Claude Code, Claude Desktop (via `mcp-remote`), VS Code, Gemini CLI, and
   more, already pointing at `http://<unraid-ip>:8080/s/<slug>/mcp`. The Claude
   **web/mobile connector** snippets need a public HTTPS URL to be usable — those
   connectors are dialed from Anthropic's cloud, so set up a tunnel first (see
   **Exposing beyond your LAN**).
3. For anything reachable off-host, put a **bearer token** on the server (Auth →
   `bearer`) so the endpoint isn't open to everyone on your network.

### Per-server setup

The `npx`, `uvx`, and `command` runners can have an optional setup script. It runs through
`/bin/sh -e -c` before every startup attempt, with the MCP child's initial environment and
working directory. Each retry runs it again, so keep it safe to rerun. Files remain where
the script writes them, but `export`, `cd`, and aliases do not carry into the child.
Docker setup belongs in the image, and remote servers have no local setup.

The script is plain server configuration, and its output goes to the authenticated server
logs. Treat both as sensitive. The official container currently runs as root, so its setup
scripts do too. Only `/data` and any other paths you explicitly mount survive a container
replacement; the npm and uv caches survive because they are configured under `/data`.
Use `MCPE_APT_PACKAGES` for global Debian packages needed by every server, not as a
per-server setup script. Those packages are installed again after container replacement.

Setup and readiness each receive their own `MCPE_START_TIMEOUT_S` window per attempt.
Failed setup, bridge launch or readiness, and exits before a stable run retry the whole
attempt after 2, 4, 8, then 16 seconds, capped at 16. The default budget is 5 attempts.
After 60 seconds of uninterrupted running the budget resets. Stop cancels startup or a pending retry. An
enabled server that ends in Failed or Unhealthy can be retried from the UI without
changing its configuration, or with `POST /api/servers/{id}/retry`.

## Docker runner (opt-in, root-equivalent)

mcpelevator can also run MCP servers packaged as **Docker images** (e.g.
`ghcr.io/github/github-mcp-server`). This is **OFF by default and root-equivalent**: it
launches images on a Docker daemon, and anything that can reach the Docker socket is
effectively root on the host. Only enable it if you trust every image you run.

To turn it on in the template (both steps are required):

1. Set **Docker Runner (root-equivalent)** (`MCPE_DOCKER_RUNNER`) to `true`. This seeds the
   `docker_runner` setting on first boot; the Settings toggle is authoritative afterwards.
2. Add the **Docker socket mount** manually — in the container's edit page click **Add another
   Path, Port, Variable, Label or Device**, choose **Path**, and set both **Container Path** and
   **Host Path** to `/var/run/docker.sock`. This is the *sibling-container* model: launched
   images talk to Unraid's own Docker daemon. (The template deliberately doesn't ship this mount —
   an empty socket path would generate an invalid bind and fail the container for everyone.)

Once enabled, paste an `mcpServers` docker config (e.g. `docker run … <image>`) or install
an **OCI** catalog entry, then start it like any other server. mcpelevator stores the
canonical shape and synthesizes a **hardened** `docker run` (`--rm --init --cap-drop ALL
--security-opt no-new-privileges --pids-limit` + a memory cap, secrets passed by name, and a
label it uses to reap orphaned containers). Import surfaces a warning for any `docker run`
option it drops (host mounts, `--network`, `--env-file`, `--platform`, …).

**Stronger isolation:** instead of the host socket, run a separate `docker:dind` daemon and
point mcpelevator at it with `DOCKER_HOST` — launched images then never touch Unraid's own
daemon. **Security:** a plaintext `tcp://…:2375` endpoint is *unauthenticated* — anyone who can
reach it controls that daemon (root-equivalent). Only use `2375` on a strictly private,
non-routable Docker network shared by just these two containers, and **never publish or
port-forward it**. For anything less contained, front dind with **TLS on `2376`**
(`DOCKER_HOST=tcp://<dind-host>:2376` + `DOCKER_TLS_VERIFY=1` and certs). See the project's
`docker-compose.yml` and [docs/security.md](security.md) for the full model and both isolation
options.

## Updating

The template tracks `ghcr.io/pacnpal/mcpelevator:latest`. Unraid's built-in update check
(**Docker → Check for Updates**) picks up new releases; apply the update and the
reconciler restarts your enabled servers automatically. Pin a specific version by
changing the repository tag (e.g. `ghcr.io/pacnpal/mcpelevator:1.1.0`) — published tags
are listed on the [GHCR package page](https://github.com/pacnpal/mcpelevator/pkgs/container/mcpelevator).

## Backup

Everything lives in the appdata share: `/mnt/user/appdata/mcpelevator`. The SQLite
database (`mcpelevator.db`) holds servers, settings, and token hashes; `.npm-cache` /
`.uv-cache` are safely regenerable. The CA **Appdata Backup** plugin covers it.

## If you switch to bridge networking

It works, with one caveat: Docker's port publishing masquerades every client to the
bridge gateway IP (a private address), so the LAN gate can no longer distinguish a LAN
client from a forwarded public one. You are then leaning entirely on the control-plane
admin token and per-server bearer auth — which `allow_private_lan` requires anyway, but
keep it in mind before port-forwarding anything. Map the container port (8080) to a host
port as usual; leave `MCPE_PORT` at `8080` and remap on the host side instead.

## Exposing beyond your LAN

Don't port-forward the raw port. To reach the box from outside (e.g. Claude web/mobile
away from home), use a tunnel that terminates auth — see
[claude-web-exposure.md](claude-web-exposure.md) for three concrete recipes (Cloudflare
Tunnel + Access with Managed OAuth, a public HTTPS tunnel + mcpelevator's built-in
bearer auth, or mcpelevator's OAuth resource server with your own authorization server). Set
`MCPE_PUBLIC_BASE_URL` to the tunnel URL so the copy menu hands out the right addresses.

## Environment variables (template reference)

| Variable | Default | Meaning |
|---|---|---|
| `MCPE_PORT` | `8080` | Port the control plane binds (host networking: this *is* the host port) |
| `MCPE_ALLOW_PRIVATE_LAN` | `true` (template) | First-boot seed for the LAN-access setting; the Settings toggle is authoritative afterwards |
| `MCPE_ADMIN_TOKEN` | _(unset)_ | Break-glass admin token, always accepted on `/api` |
| `MCPE_MINT_ADMIN_TOKEN` | `false` | Mint + print a fresh admin token on boot (recovery); unset after use |
| `MCPE_PUBLIC_BASE_URL` | _(unset)_ | Absolute URL clients use when the box sits behind a tunnel/reverse proxy |
| `MCPE_ALLOWED_HOSTS` | _(unset)_ | Extra hostnames the Host/Origin guard trusts (e.g. a reverse-proxy hostname) |
| `MCPE_DOCKER_RUNNER` | `false` | **Root-equivalent, opt-in.** First-boot seed for the docker runner (launch image-packaged MCP servers). Needs a Docker endpoint — either the host socket mounted (sibling model) or a `DOCKER_HOST` pointing at a separate dind daemon — see [Docker runner](#docker-runner-opt-in-root-equivalent) |
| `MCPE_APT_PACKAGES` | _(unset)_ | Global, space-separated extra Debian packages installed before mcpelevator starts, not per-server setup. A failed install warns and boot continues; packages are reinstalled after container replacement |
| `MCPE_MAX_RUNNING` | `50` | Cap on concurrently running MCP servers |
| `MCPE_START_TIMEOUT_S` | `120` | Separate timeout for setup and readiness on each startup attempt |
| `MCPE_RESTART_BUDGET` | `5` | Startup attempts before Failed; retry waits are 2/4/8/16 seconds and cap at 16 |
| `MCPE_RESTART_STABLE_S` | `60` | Uninterrupted running time before the retry budget is restored |

The full list, including reverse-proxy knobs (`MCPE_TRUSTED_PROXIES`,
`MCPE_TRUST_DOCKER_HOST`), is in the [README](../README.md#configuration-env-vars-prefix-mcpe_).

## Troubleshooting

- **403 on `http://<unraid-ip>:8080`** — LAN access is off (or the request isn't coming
  from a private-network peer). Check the container log for the auth banner; verify
  `MCPE_ALLOW_PRIVATE_LAN=true` was set on *first* boot, or toggle **Allow access from
  devices on your local network** in Settings via a loopback path (SSH tunnel:
  `ssh -L 8080:127.0.0.1:8080 root@<unraid-ip>`, then `http://localhost:8080` —
  substitute your `MCPE_PORT` for both `8080`s if you changed it).
  Only private-IP-literal hosts qualify — a hostname that resolves to a LAN IP is
  rejected by design; add such a hostname via `MCPE_ALLOWED_HOSTS` instead.
- **Login screen but no token** — the token is printed only when it's first minted.
  Scroll the log back to the first start, or use one of the recovery env vars above.
- **Port already in use** — host networking means a real bind conflict; change
  `MCPE_PORT` in the template.
- **A server is slow to start the first time** — `npx`/`uvx` download the package on
  first run. The cache persists in `/data`, so subsequent starts are fast. Raise
  `MCPE_START_TIMEOUT_S` for very large setup scripts or packages; setup and readiness
  each receive the full timeout.
