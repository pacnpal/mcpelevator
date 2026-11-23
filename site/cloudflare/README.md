# pacnp.al/mcpelevator

This Worker builds the project site and serves it at `https://pacnp.al/mcpelevator/` as
static assets. It runs on a Cloudflare route, and route Workers run before whatever serves
the rest of the zone. So the personal site at the root keeps working, and nothing outside
`/mcpelevator` is touched.

GitHub Pages (`.github/workflows/pages.yml`) still publishes the same files at
`pacnpal.github.io/mcpelevator/`.

## Deploy

Deploys happen on their own. Cloudflare Workers Builds is connected to this repository
and runs `npx wrangler deploy` from `site/cloudflare` on every push to `main` that changes
`site/`, `docs/screenshots/` or `assets/` (the trigger "Deploy site Worker from main" on
the `mcpelevator-site` Worker). Wrangler runs the `[build]` command in `wrangler.toml`
first. That command copies `site/index.html`, the screenshots and the logo into `dist/`,
and then the deploy uploads them.

To build and deploy by hand, you need a Cloudflare login with access to the `pacnp.al`
zone:

```bash
cd site/cloudflare
npx wrangler login     # one time, opens a browser
npx wrangler deploy    # builds dist/, then uploads it
```

Then check:

```bash
curl -sI https://pacnp.al/mcpelevator      # 307 to /mcpelevator/
curl -sI https://pacnp.al/mcpelevator/     # 200
curl -sI https://pacnp.al/                 # still the personal site
```
