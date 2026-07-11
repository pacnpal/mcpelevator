# UI screenshots

These PNGs are **generated, not hand-authored** — don't edit them by hand.

They're captured by [shot-scraper](https://shot-scraper.datasette.io) against a
locally-served instance seeded with a demo set of servers, so the dashboard shows
real running / failed / stopped state instead of an empty first-run screen.

| File | Page |
| --- | --- |
| `servers.png` | Dashboard — supervised servers with live state |
| `servers-mobile.png` | Dashboard at phone width |
| `add-server.png` | Add-a-server form |
| `catalog-browse.png` | Browse the registry |
| `catalog-search.png` | Registry search results |
| `settings.png` | Access tokens + network security |

## Regenerating

Automatically, via [`.github/workflows/screenshots.yml`](../../.github/workflows/screenshots.yml)
— on demand (**Actions → Screenshots → Run workflow**) or on a push to `main` that
touches `frontend/`, `backend/`, or the screenshot config. It builds the SPA, boots
the backend, seeds the demo servers, runs shot-scraper, compresses the PNGs with
[Oxipng](https://github.com/shssoichiro/oxipng), and commits the result back.

Locally:

```bash
make build                              # -> frontend/build
cd backend && uv sync && cd ..
bash scripts/screenshots-serve.sh       # backend up on :8080 + demo servers seeded
shot-scraper multi shots.yml --retina   # writes the PNGs here
```

The shot definitions live in [`shots.yml`](../../shots.yml); the seed/serve logic
in [`scripts/screenshots-serve.sh`](../../scripts/screenshots-serve.sh).
