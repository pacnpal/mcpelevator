# pacnp.al/mcpelevator

The project site builds and deploys to GitHub Pages (`.github/workflows/pages.yml`),
which serves it at `pacnpal.github.io/mcpelevator/`. This Worker puts the same site at
`https://pacnp.al/mcpelevator/`.

It runs on a Cloudflare route, and route Workers run before whatever serves the rest of
the zone (another Worker on a Custom Domain is treated as the origin). So the personal
site at the root keeps working, and nothing outside `/mcpelevator` is touched.

## Deploy

Deploys are automatic. Cloudflare Workers Builds watches this repository and runs
`npx wrangler deploy` from `site/cloudflare` on every push to `main` that changes a file
in this folder (the trigger "Deploy site Worker from main" on the `mcpelevator-site`
Worker; this README is excluded). Site content updates don't need a deploy at all, since
the Worker fetches each page from GitHub Pages.

To deploy by hand, you need a Cloudflare login with access to the `pacnp.al` zone:

```bash
cd site/cloudflare
npx wrangler login     # one time, opens a browser
npx wrangler deploy
```

### Or through the cloudflare-api MCP server

The mcpelevator instance hosts Cloudflare's own MCP server as `cloudflare-api`
(`https://mcp.pacnpal.com/s/cloudflare-api/mcp`), signed in to the account that owns
`pacnp.al`. Its `execute` tool can do the same deploy with no local `wrangler` login. Ask
an agent connected to it to:

1. Upload `worker.js` as the module Worker `mcpelevator-site`: a multipart `PUT` to
   `/accounts/{account_id}/workers/scripts/mcpelevator-site`, with metadata
   `{"main_module": "worker.js", "compatibility_date": "2026-03-01"}` (keep the date in
   step with `wrangler.toml`).
2. Make sure the `pacnp.al` zone has both routes from `wrangler.toml` pointing at that
   script (`GET /zones/{zone_id}/workers/routes`). `POST` any that are missing, and `PUT`
   any that exist but point at a different script, using the route's id.

If `cloudflare-api` shows as failed with "needs OAuth sign-in", retry it from the
mcpelevator UI first, since its stored tokens often still work. If it fails again, the
refresh token has lapsed: open the server page, select **Re-authenticate**, and finish the
sign-in in the browser before deploying.

Then check:

```bash
curl -sI https://pacnp.al/mcpelevator      # 301 to /mcpelevator/
curl -sI https://pacnp.al/mcpelevator/     # 200 from GitHub Pages
curl -sI https://pacnp.al/                 # still the personal site
```

Responses are edge-cached for five minutes, so a fresh Pages deploy can take that long to
show up.
