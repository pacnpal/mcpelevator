# Catalog — browsing upstream MCP directories

The catalog lets operators **discover** MCP servers from public directories and
**install** them through the normal review-and-create flow. It is a small plugin
architecture: each upstream directory is a self-contained `Source`, and the rest of the
app (API + SPA) is source-agnostic.

mcpelevator never persists directory data. A catalog request resolves an upstream
document on demand into a deterministic, reviewable **draft** (`runner`/`command`/`args`/
`env`); the operator reviews it and posts it to `POST /api/servers` like any other
server, tagged with `source="catalog:<id>"`.

## Layout

| File | Responsibility |
| --- | --- |
| `base.py` | The `Source` protocol + shared infra: fail-fast `get_json`, `TTLCache`, `clamp_limit`, `CatalogUpstreamError`. |
| `mapping.py` | **Pure** launch-spec core for *package-based* registries: `package_draft()` turns a `server.json` package (npm/pypi/…) into a draft. No I/O, no clocks — same input → same output. |
| `official.py` | `OfficialSource` — the MCP Registry (`registry.modelcontextprotocol.io`). Auto-install. |
| `glama.py` | `GlamaSource` — the Glama directory. Discovery-only (no launch spec ⇒ manual scaffold). |
| `registry.py` | **SSOT**: the list of available sources. The only place that enumerates them. |

The API (`app/api/catalog.py`) and the SPA (`/catalog`) read from `registry.py`; they
never name a source. The normalized response shapes live once in `app/api/schemas.py`
(`CatalogServer`, `CatalogList`, `CatalogDraft`, `CatalogDetail`, `CatalogSource`).

## The `Source` contract

A source is any object with these attributes/methods (see `base.Source`):

```python
class Source(Protocol):
    id: str               # stable key, e.g. "official"
    label: str            # display name, e.g. "MCP Registry"
    install_support: str  # "auto" (a runnable command is derivable) | "manual" (discovery only)

    async def list_servers(self, http, *, search, cursor, limit) -> dict: ...
    #   → {"servers": [CatalogServer-dict, ...], "next_cursor": str | None}

    async def get_detail(self, http, *, id, version) -> dict: ...
    #   → a CatalogDetail-dict (server meta + drafts + remotes + notes)
```

`http` is the shared `httpx.AsyncClient` (`app.state.http`). Use `base.get_json`, which
applies a fast 15s timeout (the shared client itself has `timeout=None` for SSE
proxying) and raises `CatalogUpstreamError` on a bad upstream — let a 404 propagate as
`httpx.HTTPStatusError` so the API can answer 404.

The dicts must match the Pydantic models in `app/api/schemas.py`; normalize the
upstream's wire shape inside the source so the rest of the app sees one contract.

## Add a new registry in three steps

### 1. Write the source module — `app/catalog/acme.py`

```python
from __future__ import annotations
from typing import Any
from urllib.parse import quote
import httpx
from app.catalog import base, mapping

BASE_URL = "https://registry.acme.example"


def _list_item(entry: dict[str, Any]) -> dict[str, Any]:
    # Map ONE upstream list entry → a CatalogServer dict.
    return {
        "source": "acme",
        "id": entry["name"],            # the key get_detail() will receive
        "name": entry["name"],
        "title": entry.get("title") or entry["name"],
        "description": entry.get("description") or "",
        "version": entry.get("version"),
        "status": entry.get("status") or "active",
        "registry_types": [p["registryType"] for p in entry.get("packages", [])],
        "installable": any(p.get("registryType") in mapping.RUNNER_BY_TYPE
                           for p in entry.get("packages", [])),
        "repository_url": entry.get("repoUrl"),
        "web_url": None,
    }


def to_detail(doc: dict[str, Any]) -> dict[str, Any]:
    # Reuse the shared package→draft core when the upstream uses the server.json
    # package shape; otherwise build drafts/blank_draft() yourself.
    drafts = [mapping.package_draft(i, p) for i, p in enumerate(doc.get("packages", []))]
    return {
        "source": "acme",
        "manual_install": False,
        "notes": [],
        "server": {
            "name": doc["name"], "title": doc.get("title") or doc["name"],
            "description": doc.get("description") or "", "version": doc.get("version"),
            "status": doc.get("status") or "active",
            "repository_url": doc.get("repoUrl"), "web_url": None,
        },
        "drafts": drafts,
        "remotes": [],
    }


class AcmeSource:
    id = "acme"
    label = "Acme Registry"
    install_support = "auto"   # or "manual" for discovery-only directories

    def __init__(self) -> None:
        self._cache = base.TTLCache()

    async def list_servers(self, http, *, search, cursor, limit):
        page = base.clamp_limit(limit)
        key = f"list:{search}:{cursor}:{page}"
        if (hit := self._cache.get(key)) is not None:
            return hit
        data = await base.get_json(http, f"{BASE_URL}/servers",
                                   {"q": search, "cursor": cursor, "limit": page})
        result = {
            "servers": [_list_item(e) for e in data.get("servers", []) if isinstance(e, dict)],
            "next_cursor": data.get("nextCursor"),
        }
        self._cache.put(key, result)
        return result

    async def get_detail(self, http, *, id, version):
        url = f"{BASE_URL}/servers/{quote(id, safe='')}"
        return to_detail(await base.get_json(http, url, {}))
```

Guidelines:

- **Keep `to_detail` / `_list_item` pure** (no I/O, no clocks, no randomness) so they are
  unit-testable and deterministic. Do the fetching only in the async methods.
- **Reuse `mapping`** for npm/pypi/etc. packages instead of re-deriving `npx -y <pkg>` /
  `uvx <pkg>` — that keeps the runner mapping in one place (SSOT) and matches what the
  bridge launches.
- **Pin versions** via `mapping.pin_identifier` (already done inside `package_draft`) so
  installs are reproducible.
- For a **discovery-only** directory (no launch spec), set `install_support = "manual"`,
  return `manual_install: True`, and use `mapping.blank_draft(...)` to scaffold the name +
  env keys. See `glama.py`.

### 2. Register it — `app/catalog/registry.py`

```python
from app.catalog.acme import AcmeSource

_SOURCES: list[Source] = [
    OfficialSource(),
    GlamaSource(),
    AcmeSource(),   # ← one line; appears in the API + UI automatically
]
```

That's the only wiring. `GET /api/catalog/sources` now lists Acme, the `/catalog` page
renders a tab for it, and install routes through the same review form.

### 3. Add tests — `backend/tests/`

- Pure mapping: feed a representative upstream document to `acme.to_detail` /
  `acme._list_item` and assert the resulting draft (see `test_catalog_mapping.py`).
- API: stub `base.get_json` (the single outbound call) and drive
  `/api/catalog/servers?source=acme` + `/api/catalog/server?source=acme&id=…`
  (see `test_catalog_api.py`).

Run: `cd backend && uv run --extra dev pytest tests/test_catalog_mapping.py tests/test_catalog_api.py -q`
