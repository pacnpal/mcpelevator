# pacnp.al/mcpelevator

The project site builds and deploys to GitHub Pages (`.github/workflows/pages.yml`),
which serves it at `pacnpal.github.io/mcpelevator/`. This Worker puts the same site at
`https://pacnp.al/mcpelevator/`.

It runs on a Cloudflare route, and route Workers run before whatever serves the rest of
the zone (another Worker on a Custom Domain is treated as the origin). So the personal
site at the root keeps working, and nothing outside `/mcpelevator` is touched.

## Deploy

Needs a Cloudflare login with access to the `pacnp.al` zone. Run it once, and again only
if `worker.js` or `wrangler.toml` changes. Site content updates don't need a redeploy,
since the Worker fetches each page from GitHub Pages.

```bash
cd site/cloudflare
npx wrangler login     # one time, opens a browser
npx wrangler deploy
```

Then check:

```bash
curl -sI https://pacnp.al/mcpelevator      # 301 to /mcpelevator/
curl -sI https://pacnp.al/mcpelevator/     # 200 from GitHub Pages
curl -sI https://pacnp.al/                 # still the personal site
```

Responses are edge-cached for five minutes, so a fresh Pages deploy can take that long to
show up.
