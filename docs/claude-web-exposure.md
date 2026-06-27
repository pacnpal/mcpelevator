# Exposing mcpelevator to Claude securely

This guide explains how to reach your mcpelevator instance from Claude — and why
"Claude **web**" (claude.ai in a browser) is a special, harder case than the
other Claude surfaces. It then walks two concrete, secure paths and helps you
pick.

> TL;DR
> - Claude web/mobile connect to your server from **Anthropic's cloud**, so the
>   endpoint must be a **public HTTPS URL** — `127.0.0.1:8080` is unreachable to it.
> - Claude web custom connectors support only **OAuth** or **no auth** — there is
>   **no field for a static bearer token or custom headers**.
> - mcpelevator v1 ships `none` and `bearer` auth, **not OAuth**. So Claude web
>   cannot send the bearer token that `/s/<slug>/mcp` expects.
> - **Path A** (claude.ai web **and mobile**): public HTTPS tunnel **+ an
>   OAuth-terminating edge**.
> - **Path B** (Claude Code / Desktop via local config): public HTTPS tunnel **+
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
   connector, Anthropic's servers make an outbound connection to your URL "over the
   public internet from Anthropic's IP ranges." A loopback or LAN-only address is
   invisible to them. You need a **public HTTPS URL with a valid certificate**.

2. **Claude web's auth is OAuth-or-nothing.** The claude.ai connector UI only lets
   you set an OAuth Client ID/Secret under *Advanced settings*. There is no place to
   paste a bearer token or add a custom header. mcpelevator's `bearer` provider
   returns `WWW-Authenticate: Bearer` on a 401 but advertises **no OAuth
   protected-resource metadata**, so Claude web's OAuth discovery finds no
   authorization server and the connect fails. (Claude **Code**, by contrast, can
   send a static `Authorization: Bearer` header directly.)

That is the whole reason "expose for web securely" needs more than flipping a port.

## Never do this

Do **not** just change the Docker `ports:` mapping from `127.0.0.1:8080:8080` to
`8080:8080` to "make it reachable." The default `docker-compose.yml` sets
`MCPE_TRUSTED_PROXIES` to the bridge **gateway** so host-loopback requests are
treated as loopback for the Host/Origin guard. That trust is **only safe while the
port is published to `127.0.0.1`**. Bind it off-host and a direct external request
arrives via the trusted gateway and can reach the **unauthenticated** `/api`
control plane. If you must bind off-host, drop `MCPE_TRUSTED_PROXIES` and put an
authenticating proxy in front of `/api`. The exposure recipes below keep the
socket loopback-only and let a tunnel do the public-facing part instead.

## Shared baseline (do this for either path)

Whichever path you choose, harden mcpelevator first. These are runtime settings on
the **Settings** page (or the `/api/settings` endpoint) plus two env vars:

1. **Keep the socket loopback-only.** Leave the published port at
   `127.0.0.1:8080`. The tunnel connects to it from the same host; nothing else can.
2. **Set the public URL.** `MCPE_PUBLIC_BASE_URL=https://mcp.example.com` — the
   absolute HTTPS URL clients will use. This host is always trusted by the
   Host/Origin guard, so the advertised URL won't 403 itself.
3. **Switch bind mode to `expose`.** Settings → bind mode. This widens the
   Host/Origin allowlist to the hosts you add and, with `control_plane_auth=auto`
   (the default), **requires an admin token on `/api`**.
4. **Add your public host to the allowlist** (`allowed_hosts`). DNS-rebinding
   defense — only the hostnames you list (plus the `MCPE_PUBLIC_BASE_URL` host) are
   accepted.
5. **Generate an admin (control-plane) token.** The Settings page can mint one,
   which logs you in immediately. Optionally set `MCPE_ADMIN_TOKEN` as a break-glass
   credential so you can't lock yourself out. **To flip to `expose` from the UI you
   must mint a token first** — by design.
6. **Set per-server auth to `bearer`** (Path B) and mint a token scoped to all
   servers or to one. (For Path A the OAuth edge handles user auth; see below.)

At this point the instance is hardened but still loopback-only. The tunnel makes it
reachable.

## Path A — claude.ai web (public HTTPS + OAuth edge)

This is the path if you specifically want the connector to work in the **browser**
(and mobile) at claude.ai. Because Claude web demands OAuth and mcpelevator doesn't
speak it, you put an **OAuth-terminating edge** in front. It handles the OAuth 2.1 +
PKCE login Claude initiates, then forwards the authenticated request to mcpelevator's
`/s/<slug>/mcp`. **Important:** that forwarded request carries the *edge's* identity
(e.g. Cloudflare's `Cf-Access-Jwt-Assertion`), **not** the `Authorization: Bearer
<mcpe token>` that mcpelevator's `bearer` provider checks (`backend/app/auth/bearer.py`).
So behind the edge you must give the origin auth the edge actually satisfies —
preferably **inject the mcpe bearer at the edge**, and fall back to running the server
with `none` **only if** the origin can't be reached except through the gated edge.
See step 1; on a public tunnel origin, prefer bearer injection.

Typical building blocks:

- **Cloudflare Tunnel (`cloudflared`)** — gives a public HTTPS hostname with a valid
  cert and **no inbound ports opened** on your host. It dials out to Cloudflare and
  to your local `127.0.0.1:8080`.
- **An OAuth-terminating edge that speaks OAuth *on mcpelevator's behalf*** (since
  mcpelevator v1 doesn't): Cloudflare's **Managed OAuth / MCP server portal**, or a
  self-hosted OAuth shim/proxy. Note that plain **Access for SaaS** is *not* this —
  it targets apps that implement their own OAuth flow, which mcpelevator doesn't, so
  it leaves no authorization/token endpoint for Claude to complete against.

Steps (high level):

1. Do the **Shared baseline** above, then decide how the edge reaches the backend.
   The edge forwards its own auth, not the mcpe token, so a bare `/s/<slug>/mcp`
   origin would 401 (`bearer`) or be wide open (`none`). Pick:
   - **(a) Inject the bearer — recommended for a public origin.** Keep the server on
     `bearer` and add a Worker / transform / reverse-proxy step that sets
     `Authorization: Bearer <mcpe token>` before the request reaches `/s/<slug>/mcp`.
     Safe even if the origin URL leaks, because the token is still required.
   - **(b) `none` — only if the origin is not independently reachable.** Acceptable
     *only* when the gated edge is the sole ingress, i.e. the tunnel exposes no public
     route straight to `/s/<slug>/mcp`. ⚠️ In Cloudflare's **MCP portal** model the
     backend origin stays directly addressable, and [blocked users can bypass the
     portal policy via the direct server URL](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/#add-an-mcp-server) —
     so `none` there leaves the tools open to anyone who finds the origin. Use (a), or
     also put an Access policy on the origin hostname/path itself.
2. Stand up the tunnel: `cloudflared tunnel ...` mapping `https://mcp.example.com`
   → `http://127.0.0.1:8080`. Set `MCPE_PUBLIC_BASE_URL` to the **full absolute URL
   including the scheme** (`https://mcp.example.com`, not the bare hostname — it's
   parsed as a URL), and add just the **hostname** (`mcp.example.com`) to
   `allowed_hosts`.
3. Put the OAuth edge (Cloudflare Managed OAuth / MCP portal, or your shim) in front
   of the public hostname; register mcpelevator's
   `https://mcp.example.com/s/<slug>/mcp` as the **backend origin** inside it, and
   allowlist the **claude.ai and claude.com OAuth callback URLs**.
4. In claude.ai → **Settings → Connectors → Add custom connector**, paste the URL the
   **edge** exposes to clients — for an MCP server portal that's the **portal URL**
   (`https://<portal-domain>/mcp`), *not* mcpelevator's origin `/s/<slug>/mcp` path —
   then complete the OAuth login.

> ⚠️ **Known caveat (verify before committing to this path).** As of mid-2026 there
> is an open report that claude.ai **web and mobile** fail to connect to Cloudflare
> Access "Managed OAuth" MCP portals — the *Connect* button does nothing and no
> login screen appears — while **Claude Code connects to the identical URL** fine.
> The suspected cause is a missing `WWW-Authenticate` header on the portal's 401.
> If you hit this, either use a different OAuth gateway that emits proper
> protected-resource metadata, or fall back to Path B for now. Test with a throwaway
> server before relying on it.

## Path B — Claude Code / Desktop (public HTTPS + bearer)

This is the **simplest secure path that works today**, and it's the right one if
"web" was loose and you'd accept Claude Code or **locally-configured** Claude
Desktop. These **can send a static `Authorization: Bearer` header** (Code natively;
Desktop via the local `claude_desktop_config.json` + `mcp-remote` bridge), which is
precisely what mcpelevator's built-in `bearer` provider checks — so no OAuth layer is
needed. Two surfaces are deliberately *not* here: the **mobile** app, and Desktop's
**remote connectors added through the Claude account UI** — both are dialed from
Anthropic's cloud with nowhere to set a header, just like the browser, so they need
**Path A**. (Only Desktop's *local* config path qualifies for bearer.)

1. Do the **Shared baseline** above, with each server's auth set to `bearer` and a
   token minted (scope `all` or a single server).
2. Stand up a tunnel that gives a public HTTPS URL:
   - **Cloudflare Tunnel** — public HTTPS hostname, no inbound ports.
   - **Tailscale Funnel** — also a **public** HTTPS tunnel: Tailscale documents
     Funnel as sharing a service "for anyone to access — even if they don't use
     Tailscale" (that's Funnel; `tailscale serve` is the tailnet-only variant). So it
     does **not** identity-gate the endpoint — the mcpe **bearer token remains your
     auth boundary**. Pick it for the convenient HTTPS URL, not as a substitute for auth.
   Map it to `http://127.0.0.1:8080`, set `MCPE_PUBLIC_BASE_URL`, and add the host to
   `allowed_hosts`.
3. Add the connector in your client.
   - **Claude Code** / **Codex** / **VS Code**: use mcpelevator's per-server **copy
     menu**, which emits the right snippet (it includes the `Authorization: Bearer`
     header). The URL is `https://mcp.example.com/s/<slug>/mcp`.
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
4. Verify the token is required: a request without `Authorization: Bearer` should
   get a **401**; with the wrong token a **401**; with a token scoped to a different
   server a **403**.

## Choosing

| Capability | **Path A — OAuth edge** | **Path B — bearer** |
|---|---|---|
| Works in claude.ai **web browser** | ✅ (subject to the caveat above) | ❌ (web can't send a bearer) |
| Works in Claude **mobile app** | ✅ (subject to the caveat above) | ❌ (mobile can't send a bearer) |
| Works in Claude **Code / Desktop (local config)** | ✅ | ✅ |
| Works in Claude **Desktop remote connector** (account UI) | ✅ (subject to the caveat above) | ❌ (cloud-dialed, no header) |
| Auth mechanism | OAuth 2.1 + PKCE at the edge | mcpelevator `bearer` token |
| Setup effort | Higher (tunnel **+** OAuth provider) | Lower (tunnel only) |
| Maturity today | Edge bug reported for web/mobile + Cloudflare Access | Stable, fully supported by mcpelevator v1 |

**Rule of thumb:** if you need the **browser or mobile** connector, take **Path A**
and test the OAuth edge with a throwaway server first. If Claude Code / Desktop is
acceptable, take **Path B** — it's secure, simpler, and uses only what mcpelevator
already ships.

## Security checklist before you go live

- [ ] Published port is still `127.0.0.1:8080` (tunnel does the public part).
- [ ] `MCPE_PUBLIC_BASE_URL` set to the full HTTPS URL (e.g. `https://mcp.example.com`); that host is in `allowed_hosts`.
- [ ] `bind_mode = expose`; control-plane auth enforced (admin token minted).
- [ ] `MCPE_ADMIN_TOKEN` set as break-glass (so you can't lock yourself out).
- [ ] Per-server auth is `bearer` (Path B) or fronted by the OAuth edge (Path A) — never an
      unauthenticated server reachable from the public URL.
- [ ] `MCPE_TRUSTED_PROXIES` is **not** trusting a publicly-reachable interface.
- [ ] Tested: no/invalid token → 401, wrong scope → 403, valid → connects.

## References

- [Get started with custom connectors using remote MCP — Claude Help Center](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Custom connectors only support OAuth client id/secret — no bearer/custom headers (anthropics/claude-ai-mcp #112)](https://github.com/anthropics/claude-ai-mcp/issues/112)
- [claude.ai web/mobile OAuth fails against Cloudflare Access Managed OAuth MCP portal (anthropics/claude-ai-mcp #410)](https://github.com/anthropics/claude-ai-mcp/issues/410)
- [MCP server portals — Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/)
- [Connect to remote MCP servers — Model Context Protocol](https://modelcontextprotocol.io/docs/develop/connect-remote-servers)
- mcpelevator [README → Security](../README.md#security)
