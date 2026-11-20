---
status: accepted
---

# Sign-in sessions are cookies, held to the control plane

SSO (issue #262) needs the browser to stay signed in after the identity provider sends it
back. We keep that Sign-in session in a cookie, not in a short-lived bearer token handed to
the SPA. The cookie is `HttpOnly`, `Secure` and `SameSite=Strict`, so script injected into
the page can't read it. A token the SPA keeps in memory or `localStorage` can be read by any
script running in the page, and the pasted Login token already lives in `localStorage`.

A cookie is sent by the browser on its own, which is exactly how `/api` would become
forgeable from another site. Two rules close that. Every request a Sign-in session
authenticates must also carry a CSRF header whose value is derived from that session, which
a cross-site page can't read or set; this applies to GETs too, since the SPA reaches `/api`
only through `fetch` and never by navigation. And a Sign-in session counts on `/api` only,
never on `/s`, `/g` or `/api/mcp`, so a browser that is signed in can't be made to call a
Server's tools or drive the control MCP for whoever lured it there. The proxies that forward
caller headers drop the cookie for the same reason. A request carrying `Authorization:
Bearer` is judged on that token alone and never falls back to the cookie, so token and agent
callers behave exactly as before.

## Considered options

- **Short-lived bearer handed to the SPA.** Keeps `/api` cookie-free, so no CSRF defense is
  needed, but leaves the credential readable by injected script. Rejected for that reason.
- **Cookie honored everywhere, like a Login token.** One credential for every surface, but
  every Server's tools and the control MCP would become reachable cross-site. Rejected.

## Consequences

The Host/Origin allowlist stays as a separate layer in front of all of this. A plain-http
LAN install can't use `Secure` or the `__Host-` prefix, so it gets a weaker cookie; that
trade is documented rather than blocked.
