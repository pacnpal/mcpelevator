# Exposing mcpelevator to Claude securely

This guide explains how to reach your mcpelevator instance from Claude — and why
"Claude **web**" (claude.ai in a browser) is a special, harder case than the
other Claude surfaces. It then walks two concrete, secure paths end-to-end, with
real commands and dashboard steps, and helps you pick.

> TL;DR
> - Claude web/mobile connect to your server from **Anthropic's cloud**, so the
>   endpoint must be a **public HTTPS URL** — `127.0.0.1:8080` is unreachable to it.
> - Claude web custom connectors support only **OAuth** or **no auth** — there is
>   **no field for a static bearer token or custom headers**.
> - mcpelevator v1 ships `none` and `bearer` auth, **not OAuth**. So Claude web
>   cannot send the bearer token that `/s/<slug>/mcp` expects.
> - **Path A** (claude.ai web **and mobile**): a **Cloudflare Tunnel** to your
>   loopback origin **+ a Cloudflare Access self-hosted application with Managed
>   OAuth** in front. Access becomes the OAuth provider mcpelevator doesn't have.
> - **Path B** (Claude Code / Desktop via local config): a public HTTPS tunnel **+
>   mcpelevator's built-in bearer auth**. Simplest, works today.

## The constraint, in one picture

```text
Claude Code / Desktop (local cfg) ─┐  can send Authorization: Bearer  ──►  Path B
                                   │
web / mobile / Desktop remote ─────┘  OAuth or no-auth ONLY (no headers) ──►  Path A
                                          │
                                          ▼
                          public HTTPS endpoint (Anthropic's IPs dial OUT to it)
                                          │
                                          ▼
                       tunnel / reverse proxy  ──►  mcpelevator  ──►  /s/<slug>/mcp
                       (TLS terminates here)        (loopback only)
```

Two facts drive everything below:

1. **Claude reaches your server, not the other way around.** When you add a custom
   connector, Anthropic's servers make an outbound connection to your URL over the
   public internet from Anthropic's IP ranges. A loopback or LAN-only address is
   invisible to them. You need a **public HTTPS URL with a valid certificate**.

2. **Claude web's auth is OAuth-or-nothing.** The claude.ai connector UI only lets
   you set an OAuth Client ID/Secret under *Advanced settings*. There is no place to
   paste a bearer token or add a custom header. mcpelevator's `bearer` provider
   returns `WWW-Authenticate: Bearer` on a 401 but advertises **no OAuth
   protected-resource metadata**, so Claude web's OAuth discovery finds no
   authorization server and the connect fails. (Claude **Code**, by contrast, can
   send a static `Authorization: Bearer` header directly.)

That is the whole reason "expose for web securely" needs more than flipping a port.
Path A's job is to put something in front that **does** speak OAuth on
mcpelevator's behalf; Cloudflare Access's **Managed OAuth** does exactly that.

## Never do this

Do **not** just change the Docker `ports:` mapping from `127.0.0.1:8080:8080` to
`8080:8080` to "make it reachable." The default `docker-compose.yml` sets
`MCPE_TRUSTED_PROXIES` to the bridge **gateway** so host-loopback requests are
treated as loopback for the Host/Origin guard. That trust is **only safe while the
port is published to `127.0.0.1`**. Bind it off-host and a direct external request
arrives via the trusted gateway and can reach the **unauthenticated** `/api`
control plane. If you must bind off-host, drop `MCPE_TRUSTED_PROXIES` and put an
authenticating proxy in front of `/api`. The recipes below keep the socket
loopback-only and let a tunnel do the public-facing part instead — the tunnel
daemon runs on the same host and dials `127.0.0.1:8080` locally, so nothing is
ever published to a public interface.

> **Just want it on your own LAN, not the public internet / Claude's cloud?** That's
> a different goal from this guide. Use the `allow_private_lan` setting instead — see
> [README → Security → Local network (LAN) access](../README.md#security). It lets
> private-IP devices on your network reach the box directly (with an admin token on
> `/api`), without a public tunnel.

## Shared baseline (do this for either path)

Whichever path you choose, harden mcpelevator first. These are runtime settings on
the **Settings** page (or the `/api/settings` endpoint) plus two env vars. Assume
your public hostname will be `mcp.example.com` — substitute your own throughout.

1. **Keep the socket loopback-only.** Leave the published port at
   `127.0.0.1:8080` (the Docker default). The tunnel connects to it from the same
   host; nothing else can.
2. **Set the public URL.** Add `MCPE_PUBLIC_BASE_URL=https://mcp.example.com` to the
   container environment — the **full absolute HTTPS URL including the scheme**, not
   a bare hostname (it is parsed as a URL). This host is always trusted by the
   Host/Origin guard, so the advertised URL won't 403 itself.
3. **Switch bind mode to `expose`.** Settings → bind mode. This widens the
   Host/Origin allowlist to the hosts you add and, with `control_plane_auth=auto`
   (the default), **requires an admin token on `/api`**.
4. **Add your public host to the allowlist** (`allowed_hosts`). Enter just the
   **hostname** (`mcp.example.com`), not a URL. DNS-rebinding defense — only the
   hostnames you list (plus the `MCPE_PUBLIC_BASE_URL` host) are accepted.
5. **Generate an admin (control-plane) token.** The Settings page can mint one,
   which logs you in immediately. Optionally set `MCPE_ADMIN_TOKEN` as a break-glass
   credential so you can't lock yourself out. **To flip to `expose` from the UI you
   must mint a token first** — by design.
6. **Set per-server auth.** For **Path B**, set each server to `bearer` and mint a
   token (scope `all` or one server). For **Path A**, the Access edge handles user
   auth; choose `none` or `bearer` per the trade-off in Path A step 4.

At this point the instance is hardened but still loopback-only. The tunnel makes it
reachable.

## Path A — claude.ai web & mobile (Cloudflare Tunnel + Access Managed OAuth)

This is the path when you specifically want the connector to work in the
**browser** and on **mobile** at claude.ai. The recipe has two Cloudflare pieces:

- **Cloudflare Tunnel (`cloudflared`)** publishes `https://mcp.example.com` with a
  valid certificate and **opens no inbound ports** — the daemon dials *out* to
  Cloudflare and forwards to your local `127.0.0.1:8080`.
- **A Cloudflare Access self-hosted application with Managed OAuth** sits on that
  hostname. [Managed OAuth turns Access into an OAuth 2.0 authorization server](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/):
  when a non-browser client (here, Anthropic's connector) hits the protected URL,
  Access replies **`401` with a `WWW-Authenticate` header pointing at
  `/.well-known/oauth-authorization-server`** (RFC 8414 / RFC 9728), runs the
  OAuth 2.1 + PKCE login, and only then forwards the request to mcpelevator. That
  discovery metadata is precisely what Claude web's OAuth flow needs and what
  mcpelevator's bearer provider can't supply.

Because the tunnel's only ingress for that hostname is your loopback origin, and
Access guards the hostname, there is **no separate "direct origin URL" to bypass** —
the cardinal rule below is satisfied by construction.

> The forwarded request carries the **edge's** identity (an opaque Access token
> resolved server-side; the origin sees `Cf-Access-Jwt-Assertion`), **not** the
> `Authorization: Bearer <mcpe token>` that mcpelevator's `bearer` provider checks
> (`backend/app/auth/bearer.py`). So either run mcpelevator with `none` (Access is
> the sole gate) or keep `bearer` and inject the header (step 4). **Cardinal rule:**
> the mcpe origin — and any token-injection hop — must be reachable *only* through
> the authenticated Access gate, never as a bare public URL.

### Step 1 — Shared baseline

Do the **Shared baseline** above. For Path A you can leave per-server auth at
`none` (decide in step 4).

### Step 2 — Stand up the Cloudflare Tunnel

You need a domain on Cloudflare (the zone must be active in your account). Two
equivalent ways to create the tunnel:

**Option A — dashboard-managed (simplest; recommended for a home/self-hosted box).**
In the [Cloudflare dashboard](https://dash.cloudflare.com/) go to
**Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel** →
**Cloudflared**. Name it (e.g. `mcpelevator`), then **copy the install command** the
dashboard shows for your OS and run it on the box. On Linux it looks like:

```bash
cloudflared service install eyJ...<connector-token>...
```

For Docker, run the connector alongside mcpelevator (host networking so it can reach
`127.0.0.1:8080`):

```bash
docker run -d --name cloudflared --network host \
  cloudflare/cloudflared:latest tunnel run --token eyJ...<connector-token>...
```

Wait for the tunnel to show **Healthy**, then **Routes** tab → **Add route** →
**Published application**: set **Subdomain** `mcp`, **Domain** `example.com`,
**Service** type **HTTP**, **URL** `localhost:8080`. Save. Cloudflare creates the
`mcp.example.com` DNS record automatically.

**Option B — locally-managed (`config.yml`).** If you prefer a config file under
version control:

```bash
cloudflared tunnel login                       # opens a browser; writes cert.pem
cloudflared tunnel create mcpelevator          # prints a Tunnel UUID + credentials JSON
cloudflared tunnel route dns mcpelevator mcp.example.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <Tunnel-UUID>
credentials-file: /root/.cloudflared/<Tunnel-UUID>.json
ingress:
  - hostname: mcp.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404            # required catch-all
```

Run it (install as a service for persistence with `cloudflared service install`):

```bash
cloudflared tunnel run mcpelevator
```

Either way you should now be able to reach mcpelevator at
`https://mcp.example.com` over HTTPS. **Don't add the connector to Claude yet** —
right now the origin is publicly reachable with whatever per-server auth you set.
Step 3 puts Access in front.

### Step 3 — Put Access (Managed OAuth) in front

Create a self-hosted Access application on the tunnel hostname, then turn on
Managed OAuth ([secure MCP servers with Access](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/)):

1. **Zero Trust** → **Access controls** → **Applications** → **Add an application**
   → **Self-hosted**.
2. Add **`mcp.example.com`** as the application's public hostname (the exact tunnel
   hostname from step 2).
3. **Add an Access policy** that defines who may log in — e.g. an **Allow** policy
   with an **Emails** rule listing your address, or **Emails ending in** your
   domain. (Configure an identity provider first under
   **Settings → Authentication** if you haven't; Cloudflare's own login works as a
   default IdP.)
4. Open the application's **Advanced settings** and toggle **Managed OAuth** **on**,
   then **Save**. This is the piece that makes Access emit OAuth discovery metadata
   and complete the PKCE flow for Claude.

Verify the gate is live: an unauthenticated request to
`https://mcp.example.com/s/<slug>/mcp` should now return **`401`** with a
`WWW-Authenticate` header (not your tool list, and not a `302` HTML redirect):

```bash
curl -i https://mcp.example.com/s/<slug>/mcp
# expect: HTTP/2 401 ... www-authenticate: Bearer resource_metadata="https://.../.well-known/..."
```

### Step 4 — Choose how Access reaches mcpelevator

The forwarded request won't carry an mcpe bearer token, so pick one:

- **`none` (simplest, recommended here).** Leave each server's auth at `none`.
  Access is the sole gate and the origin is only reachable through the gated tunnel,
  so this is safe **as long as** you used a self-hosted Access *application* on the
  hostname (above). The mcpe Host/Origin guard still applies as defense in depth.
- **`bearer` + header injection (defense in depth).** Keep servers on `bearer` and
  have Cloudflare add the header *after* the Access gate. The simplest grounded way
  is a [Request Header Transform Rule](https://developers.cloudflare.com/rules/transform/request-header-modification/)
  that, for `http.host eq "mcp.example.com"`, **sets** `Authorization` to
  `Bearer <YOUR_MCPE_TOKEN>` (a Worker bound to the route with the token as a
  secret is the more secret-safe alternative). ⚠️ This only helps because Access
  already gated the hostname; never put the injection on an ungated public hostname,
  or a direct hit gets the token injected and bypasses OAuth.

### Step 5 — Add the connector in Claude

In claude.ai → **Settings** → **Connectors** → **Add custom connector**, paste:

```
https://mcp.example.com/s/<slug>/mcp
```

Click connect; Claude's OAuth discovery hits the `WWW-Authenticate` metadata,
Access shows your IdP login, and after you approve, the connector goes live. The
same connector then works in the **mobile** app.

> ### Alternative: an MCP server portal (and why it's the second choice)
> Cloudflare also offers [MCP server portals](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/),
> which front one or more MCP servers behind a single `https://<sub>.<domain>/mcp`
> endpoint. Two reasons it's not the primary recipe here:
> 1. **Bypass risk.** Per Cloudflare's own docs, "blocked users can still connect
>    to the server (and bypass your Access policies) by using its direct URL" unless
>    you also [configure Access as the server's OAuth provider](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/).
>    The self-hosted-app recipe above avoids this because the origin is private.
> 2. **Reported web/mobile bug.** As of mid-2026 there is an open report that
>    claude.ai **web and mobile** fail to connect to Access "Managed OAuth" MCP
>    *portals* — the *Connect* button does nothing — while **Claude Code connects to
>    the identical URL** fine
>    ([anthropics/claude-ai-mcp #410](https://github.com/anthropics/claude-ai-mcp/issues/410)).
>    The suspected cause is a missing `WWW-Authenticate` header on the portal's 401.
>
> If you use a portal anyway, register mcpelevator's
> `https://mcp.example.com/s/<slug>/mcp` as the backend MCP server, enable Managed
> OAuth on the portal, and give clients the **portal URL** (`https://<sub>.<domain>/mcp`),
> not the origin path. **Test with a throwaway server before relying on it.**

## Path B — Claude Code / Desktop (public HTTPS + bearer)

This is the **simplest secure path that works today**, and it's the right one if
"web" was loose and you'd accept Claude Code or **locally-configured** Claude
Desktop. These **can send a static `Authorization: Bearer` header** (Code natively;
Desktop via the local `claude_desktop_config.json` + `mcp-remote` bridge), which is
precisely what mcpelevator's built-in `bearer` provider checks — so no OAuth layer
is needed. Two surfaces are deliberately *not* here: the **mobile** app, and
Desktop's **remote connectors added through the Claude account UI** — both are
dialed from Anthropic's cloud with nowhere to set a header, just like the browser,
so they need **Path A**. (Only Desktop's *local* config path qualifies for bearer.)

### Step 1 — Shared baseline

Do the **Shared baseline** above, with each server's auth set to `bearer` and a
token minted (scope `all` or a single server).

### Step 2 — Stand up a public HTTPS tunnel

Either tunnel gives you a public HTTPS URL mapped to `http://127.0.0.1:8080`:

- **Cloudflare Tunnel** — exactly as in **Path A step 2** (dashboard-managed or
  `config.yml`), but **skip the Access application** — the mcpe bearer token is your
  auth boundary here. After it's up, set `MCPE_PUBLIC_BASE_URL` and add the host to
  `allowed_hosts` (Shared baseline steps 2 and 4).
- **Tailscale Funnel** — also a **public** HTTPS tunnel. Tailscale documents Funnel
  as sharing a service "for anyone to access — even if they don't use Tailscale"
  (that's Funnel; `tailscale serve` is the tailnet-only variant). Funnel does **not**
  identity-gate the endpoint — anyone with the URL can reach it, so the mcpe
  **bearer token remains your auth boundary**. To expose port 8080:

  ```bash
  tailscale funnel 8080
  # serves https://<device>.<tailnet>.ts.net  → http://127.0.0.1:8080
  ```

  Use the printed `*.ts.net` host as `MCPE_PUBLIC_BASE_URL` / `allowed_hosts`.

### Step 3 — Add the connector in your client

- **Claude Code** / **Codex** / **VS Code**: use mcpelevator's per-server **copy
  menu**, which emits the right snippet (it includes the `Authorization: Bearer`
  header). The URL is `https://mcp.example.com/s/<slug>/mcp`. For Claude Code the CLI
  equivalent is:

  ```bash
  claude mcp add --transport http <slug> https://mcp.example.com/s/<slug>/mcp \
    --header "Authorization: Bearer <YOUR_TOKEN>"
  ```

- **Claude Desktop (local config)**: the copy menu's `mcpServers` JSON is the
  remote-HTTP form (`{"type":"http", …}`) that Code/VS Code accept — Desktop's
  `claude_desktop_config.json` runs **stdio** servers, so wrap the URL in the
  `mcp-remote` bridge and inject the header with `--header`:

  ```json
  {
    "mcpServers": {
      "<slug>": {
        "command": "npx",
        "args": [
          "-y", "mcp-remote",
          "https://mcp.example.com/s/<slug>/mcp",
          "--header", "Authorization: Bearer <YOUR_TOKEN>"
        ]
      }
    }
  }
  ```

### Step 4 — Verify the token is required

```bash
# no token → 401
curl -i https://mcp.example.com/s/<slug>/mcp

# wrong token → 401
curl -i -H "Authorization: Bearer wrong" https://mcp.example.com/s/<slug>/mcp

# token scoped to a different server → 403
curl -i -H "Authorization: Bearer <OTHER_SERVER_TOKEN>" https://mcp.example.com/s/<slug>/mcp
```

## Choosing

| Capability | **Path A — Access Managed OAuth** | **Path B — bearer** |
|---|---|---|
| Works in claude.ai **web browser** | ✅ (self-hosted-app recipe) | ❌ (web can't send a bearer) |
| Works in Claude **mobile app** | ✅ | ❌ (mobile can't send a bearer) |
| Works in Claude **Code / Desktop (local config)** | ✅ | ✅ |
| Works in Claude **Desktop remote connector** (account UI) | ✅ | ❌ (cloud-dialed, no header) |
| Auth mechanism | OAuth 2.1 + PKCE at the Access edge | mcpelevator `bearer` token |
| Setup effort | Higher (tunnel **+** Access app + Managed OAuth) | Lower (tunnel only) |
| Maturity today | Solid via self-hosted app; **avoid** the MCP *portal* for web (bug #410) | Stable, fully supported by mcpelevator v1 |

**Rule of thumb:** if you need the **browser or mobile** connector, take **Path A**
using a **self-hosted Access application + Managed OAuth** (not the MCP portal), and
test with a throwaway server first. If Claude Code / Desktop is acceptable, take
**Path B** — it's secure, simpler, and uses only what mcpelevator already ships.

## Security checklist before you go live

- [ ] Published port is still `127.0.0.1:8080` (tunnel does the public part).
- [ ] `MCPE_PUBLIC_BASE_URL` set to the full HTTPS URL (e.g. `https://mcp.example.com`); that host is in `allowed_hosts`.
- [ ] `bind_mode = expose`; control-plane auth enforced (admin token minted).
- [ ] `MCPE_ADMIN_TOKEN` set as break-glass (so you can't lock yourself out).
- [ ] **Path A:** an Access self-hosted application guards the hostname **and** Managed OAuth is **on**; a `curl` to `/s/<slug>/mcp` returns `401` with `WWW-Authenticate`, not your tool list.
- [ ] **Path A with `bearer`:** the header-injection rule fires **only** behind the Access gate, never on an ungated hostname.
- [ ] **Path B:** per-server auth is `bearer` with a minted token; no server is reachable unauthenticated from the public URL.
- [ ] `MCPE_TRUSTED_PROXIES` is **not** trusting a publicly-reachable interface.
- [ ] Tested: no/invalid token → 401, wrong scope → 403, valid → connects.

## References

- [Get started with custom connectors using remote MCP — Claude Help Center](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Custom connectors only support OAuth client id/secret — no bearer/custom headers (anthropics/claude-ai-mcp #112)](https://github.com/anthropics/claude-ai-mcp/issues/112)
- [claude.ai web/mobile OAuth fails against Cloudflare Access Managed OAuth MCP portal (anthropics/claude-ai-mcp #410)](https://github.com/anthropics/claude-ai-mcp/issues/410)
- [Create a remotely-managed Cloudflare Tunnel — Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)
- [Create a locally-managed Cloudflare Tunnel (`config.yml`) — Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/)
- [Managed OAuth — Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/)
- [Secure MCP servers with Cloudflare Access — Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/)
- [MCP server portals — Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/)
- [Request Header Transform Rules — Cloudflare docs](https://developers.cloudflare.com/rules/transform/request-header-modification/)
- [Tailscale Funnel — Tailscale docs](https://tailscale.com/kb/1223/funnel)
- [Connect to remote MCP servers — Model Context Protocol](https://modelcontextprotocol.io/docs/develop/connect-remote-servers)
- mcpelevator [README → Security](../README.md#security)
