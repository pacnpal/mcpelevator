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
> - **Path A** (claude.ai web): public HTTPS tunnel **+ an OAuth-terminating edge**.
> - **Path B** (Claude Code / Desktop / mobile): public HTTPS tunnel **+
>   mcpelevator's built-in bearer auth**. Simplest, works today.

## The constraint, in one picture

```
Claude Code / Desktop / mobile ──┐  can send Authorization: Bearer  ──►  Path B
                                 │
claude.ai web ───────────────────┘  OAuth or no-auth ONLY (no headers) ──►  Path A
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
speak it, you put an **OAuth-terminating reverse proxy** in front. It handles the
OAuth 2.1 + PKCE login Claude initiates, then forwards authenticated requests to
mcpelevator's `/s/<slug>/mcp` (injecting the bearer mcpelevator expects, or running
the server with `none` auth behind a network it fully controls).

Typical building blocks:

- **Cloudflare Tunnel (`cloudflared`)** — gives a public HTTPS hostname with a valid
  cert and **no inbound ports opened** on your host. It dials out to Cloudflare and
  to your local `127.0.0.1:8080`.
- **Cloudflare Access** in front of it as the OAuth provider — e.g. an **MCP server
  portal** or **Access for SaaS**. Cloudflare Access supports both unauthenticated
  MCP servers and OAuth-secured ones, and issues the tokens Claude's connector flow
  needs.

Steps (high level):

1. Do the **Shared baseline** above. For Path A you may run the target server with
   `bearer` (the proxy injects the header) or `none` if and only if the proxy fully
   gates access.
2. Stand up the tunnel: `cloudflared tunnel ...` mapping `https://mcp.example.com`
   → `http://127.0.0.1:8080`. Point `MCPE_PUBLIC_BASE_URL` at that hostname and add
   it to `allowed_hosts`.
3. Put Cloudflare Access (Managed OAuth / MCP portal) in front of the public
   hostname and allowlist the **claude.ai and claude.com OAuth callback URLs**.
4. In claude.ai → **Settings → Connectors → Add custom connector**, paste
   `https://mcp.example.com/s/<slug>/mcp`, and complete the OAuth login.

> ⚠️ **Known caveat (verify before committing to this path).** As of mid-2026 there
> is an open report that claude.ai **web and mobile** fail to connect to Cloudflare
> Access "Managed OAuth" MCP portals — the *Connect* button does nothing and no
> login screen appears — while **Claude Code connects to the identical URL** fine.
> The suspected cause is a missing `WWW-Authenticate` header on the portal's 401.
> If you hit this, either use a different OAuth gateway that emits proper
> protected-resource metadata, or fall back to Path B for now. Test with a throwaway
> server before relying on it.

## Path B — Claude Code / Desktop / mobile (public HTTPS + bearer)

This is the **simplest secure path that works today**, and it's the right one if
"web" was loose and you'd accept Claude Code, Claude Desktop, or the mobile app.
These surfaces **can send a static `Authorization: Bearer` header**, which is
exactly what mcpelevator's built-in `bearer` provider checks — so no OAuth layer is
needed.

1. Do the **Shared baseline** above, with each server's auth set to `bearer` and a
   token minted (scope `all` or a single server).
2. Stand up a tunnel that gives a public/edge HTTPS URL:
   - **Cloudflare Tunnel** — public HTTPS hostname, no inbound ports.
   - **Tailscale Funnel** — HTTPS URL reachable from outside your tailnet; good if
     you want it tied to your Tailscale identity.
   Map it to `http://127.0.0.1:8080`, set `MCPE_PUBLIC_BASE_URL`, and add the host to
   `allowed_hosts`.
3. Add the connector in your client. mcpelevator's per-server **copy menu** emits
   the right snippet per client (Claude Code, Codex, `mcpServers`/VS Code JSON, raw
   URL). For Claude Desktop, use the `mcp-remote` bridge with the URL +
   `Authorization: Bearer <token>` header. The URL is
   `https://mcp.example.com/s/<slug>/mcp`.
4. Verify the token is required: a request without `Authorization: Bearer` should
   get a **401**; with the wrong token a **401**; with a token scoped to a different
   server a **403**.

## Choosing

| | **Path A — OAuth edge** | **Path B — bearer** |
|---|---|---|
| Works in claude.ai **web browser** | ✅ (subject to the caveat above) | ❌ (web can't send a bearer) |
| Works in Claude **Code / Desktop / mobile** | ✅ | ✅ |
| Auth mechanism | OAuth 2.1 + PKCE at the edge | mcpelevator `bearer` token |
| Setup effort | Higher (tunnel **+** OAuth provider) | Lower (tunnel only) |
| Maturity today | Edge bug reported for web/mobile + Cloudflare Access | Stable, fully supported by mcpelevator v1 |

**Rule of thumb:** if you truly need the **browser** connector, take **Path A** and
test the OAuth edge with a throwaway server first. If Claude Code / Desktop / mobile
is acceptable, take **Path B** — it's secure, simpler, and uses only what
mcpelevator already ships.

## Security checklist before you go live

- [ ] Published port is still `127.0.0.1:8080` (tunnel does the public part).
- [ ] `MCPE_PUBLIC_BASE_URL` set to the HTTPS hostname; that host is in `allowed_hosts`.
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
