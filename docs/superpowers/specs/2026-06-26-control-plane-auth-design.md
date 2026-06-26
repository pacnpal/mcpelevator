# Control-plane auth design

Date: 2026-06-26
Status: approved, ready for implementation plan

## Problem

The control plane (`/api/*`, including `/api/tokens` and `/api/settings`) has no per-request authentication. Today it is guarded only by the Host/Origin allowlist in `backend/app/main.py` (`_control_plane_allowlist`). That allowlist is a DNS-rebinding defense for browsers. It does nothing against a direct network attacker who can set an arbitrary `Host` header. Such an attacker can mint tokens and change settings.

Codex raised this on PR #1 (comment 3482461469, P1). It was deferred because real auth on `/api` also has to keep the same-origin SPA working. The SPA currently calls `/api` with no credentials.

This design adds per-request auth to the control plane without breaking the SPA, and without degrading the local-first zero-config experience.

## Threat model

The attacker we are closing out is a direct network client that can reach the port and forge a `Host` header. Not a browser, not XSS. The Host/Origin allowlist already handles the browser/DNS-rebinding case, so we keep it.

Against a raw network client, a bearer token in the `Authorization` header is a complete gate: no token, 401. CSRF does not apply, because CSRF is a browser confused-deputy attack and a network client is not a confused browser. That is why this design uses a bearer token and not a session cookie. No CSRF machinery is needed.

The token lives in browser `localStorage`. That is fine for this threat model. The SPA is first-party with no third-party scripts, and protecting against XSS-driven token theft is explicitly not the goal here.

## Decisions

These four were chosen during brainstorming:

1. Reuse the existing `BearerProvider` / `Token` seam with a new `control` scope. No session cookies.
2. A runtime setting `control_plane_auth` with values `auto` and `always`, default `auto`. `auto` enforces when `bind_mode == "expose"` or a public URL (`MCPE_PUBLIC_BASE_URL`) is configured, so a plain local install stays zero-config.
3. The SSE log stream switches from `EventSource` to a fetch + `ReadableStream` reader so it sends the `Authorization` header like every other call. No token in any URL.
4. Frontend tests use Vitest, unit-testing the auth and api lib modules. Full browser E2E is out of scope.

## Two layers, defense in depth

The control plane gets two independent checks. A request must pass both.

1. Host/Origin allowlist (`_control_plane_allowlist` middleware). Unchanged. Runs first. Loopback always passes, `expose` adds the configured hosts.
2. Bearer token (new). A router-level dependency on the control-plane routers.

The `/s` data plane and its per-server auth are untouched. `BearerProvider` and the proxy router are not modified.

## Backend design

### Token scope

Add a `scope` column to the `Token` model: `"proxy"` (default) or `"control"`. Existing tokens become `proxy`, which is correct since they were minted for `/s` access.

There is no Alembic in this repo. `init_db()` runs `SQLModel.metadata.create_all`, which does not add columns to an existing table. So `init_db()` gets a small idempotent migration: check `PRAGMA table_info(token)`, and if `scope` is missing, run `ALTER TABLE token ADD COLUMN scope TEXT NOT NULL DEFAULT 'proxy'`.

Control-plane access requires `scope == "control"`. The data-plane bearer check (`BearerProvider`) is tightened to require `scope == "proxy"`, so the two scopes don't cross: a control token authenticates `/api` only and a proxy token authenticates `/s` only.

### Enforcement setting

`registry/settings.py` `DEFAULTS` gains:

```python
"control_plane_auth": "auto",  # 'auto' (required when exposed or a public URL is set) | 'always'
```

`write()` validates the value against `{"auto", "always"}`. Add a `control_plane_auth(session) -> str` accessor next to the existing ones. Surface it in `GET`/`PATCH /api/settings`.

### The gate: a router-level dependency

New module `backend/app/auth/control_plane.py`.

`enforcement_enabled(session) -> bool`:

```
cpa = control_plane_auth(session)
return cpa == "always" or (cpa == "auto" and (bind_mode(session) == "expose" or public_host_is_set()))
```

A configured public URL counts as exposed because `request_allowlist` already trusts that host, so the token gate must too, or a tunnel deployment would leave `/api` open. The decision lives in a shared `_enforced` helper, reused by the settings lock-out guard below.

`require_control_plane` is a FastAPI dependency. Logic:

1. If not `enforcement_enabled(session)`, return. This pass-through is what keeps local zero-config working.
2. Read the bearer token from the `Authorization` header.
3. If the token equals the configured break-glass env token (constant-time compare), allow. It has implicit `control` scope.
4. Otherwise hash it and look it up via `repo.get_token_by_hash`. No token or no match: raise 401 with `WWW-Authenticate: Bearer`. Token found but `scope != "control"`: raise 403.

Attach it with `include_router(..., dependencies=[Depends(require_control_plane)])` on the servers, tokens, and settings routers. Do not attach it to the health router or the new auth-status route. Both stay public.

The task's acceptance is satisfied: no token gives 401, a control token gives 200. A valid proxy token gives 403, which is the right answer for wrong scope.

### Break-glass env var

`backend/app/config.py` `Settings` gains `admin_token: str | None = None`, read from `MCPE_ADMIN_TOKEN`. When set, the dependency accepts a bearer token equal to it. This solves three things: recovery when the minted token is lost, CI and automation access, and a simple way to log in without reading container logs.

When the env token is set, startup does not mint a DB token, since enforcement can already be satisfied.

### Bootstrap and no lockout

No-lockout is deterministic, with no per-request minting. `ensure_control_token(session) -> str | None` mints a `control`-scoped token if none exists and no env admin token is configured, returning the plaintext once (else `None`). It is called only at startup, in `lifespan` after `init_db()`: if enforcement is on and it mints one, the token is logged once with the login URL; if `MCPE_ADMIN_TOKEN` is set, that is logged as in effect instead.

The runtime paths reject rather than mint:

- `PATCH /api/settings` calls `would_lock_out(session, changes)` and returns 400 if the change would turn enforcement on while no admin credential (a control token or `MCPE_ADMIN_TOKEN`) exists, so a direct API call can't strand the operator.
- `DELETE /api/tokens/{id}` refuses (409) to remove the last control token while enforcement is on (atomically, so concurrent deletes can't both slip through), so revoking can't lock you out either.
- The Settings UI disables `expose` / `always` until this browser holds a usable control token (`GET /api/auth/status` -> `authenticated`), and a "Generate admin token" button mints one (`POST /tokens` with `scope=control`) and stores it, so the operator is signed in before enabling enforcement.

### Auth status endpoint

New `GET /api/auth/status`, public (allowlist only, no token dependency). Returns:

```json
{ "enforced": true, "authenticated": false }
```

`enforced` is `enforcement_enabled(session)`. `authenticated` is whether this request carried a valid control token (header or env). The SPA calls this on load to decide whether to show the login screen, instead of guessing from 401s.

### Backend files touched

- `db/models.py`: `Token.scope`.
- `db/__init__.py`: idempotent `scope` column migration in `init_db()`.
- `db/repo.py`: `control_token_exists(session) -> bool`.
- `config.py`: `admin_token` from `MCPE_ADMIN_TOKEN`.
- `registry/settings.py`: `control_plane_auth` default, validation, accessor.
- `auth/control_plane.py`: new. `enforcement_enabled`, `require_control_plane`, `ensure_control_token`.
- `api/auth.py`: new. `GET /api/auth/status`.
- `api/tokens.py`: `POST /tokens` takes an optional `scope` (default `proxy`); `TokenInfo` and `TokenCreated` include `scope`.
- `api/settings.py`: `SettingsInfo` includes `control_plane_auth`; `PATCH` rejects (400) a change that would enable enforcement while no admin credential exists.
- `main.py`: attach `require_control_plane` to the three routers, include the auth router, run the startup bootstrap in `lifespan`.

## Frontend design

### Token store

New `lib/auth.ts`: plain `getToken`, `setToken`, `clearToken` over `localStorage['mcpe_admin_token']` (the single source of truth; the layout re-reads on navigation).

### API wrapper

`lib/api.ts`:

- Attach `Authorization: Bearer <token>` in `request()` when a token is present.
- On a 401 from `/api`, clear the token and route to `/login`.
- Replace `logStreamUrl` plus `EventSource` usage with a fetch + `ReadableStream` SSE reader that sends the `Authorization` header and parses `data:` lines. Manual reconnect with a short backoff, since fetch streams do not auto-reconnect the way `EventSource` does.

### Routes

- `routes/login/+page.svelte`: new. Paste the admin token from the logs, validate it against `/api/auth/status`, store it, redirect home. Short copy explaining where the token comes from.
- `routes/+layout`: on load, call `/api/auth/status`. If `enforced && !authenticated`, redirect to `/login` unless already there. Add a log-out affordance.
- `routes/settings/+page.svelte`: add the `control_plane_auth` selector (`auto` / `always`) and a "Generate admin token" action that mints a control token, shows it once, and stores it. Disable `expose` / `always` until this browser holds a usable control token.
- `routes/server/[id]/+page.svelte`: switch the live log view to the new fetch-stream reader.

### Types

`lib/types.ts`: `SettingsInfo.control_plane_auth`, `TokenInfo.scope`, a new `AuthStatus` type.

## Tests

### Backend (pytest, TestClient)

New `backend/tests/test_control_plane_auth.py`:

- local `auto`, no token, `GET /api/servers` returns 200. Zero-config preserved.
- expose `auto`, allowed host, no token, returns 401.
- expose `auto`, valid control token, returns 200.
- expose `auto`, valid proxy token, returns 403. Wrong scope.
- `control_plane_auth = always` in local mode: 401 without a token, 200 with a control token.
- `GET /api/health` returns 200 with no token even when enforced (loopback host).
- `GET /api/auth/status` reports `enforced` and `authenticated` correctly.
- `PATCH /api/settings` to enable enforcement with no control token is rejected (400); `DELETE` of the last control token under enforcement is rejected (409).
- `MCPE_ADMIN_TOKEN` is accepted as a control token.
- Allowlist still wins: a bad `Host` returns 403 even with a valid token, proving the two layers run in the right order.

Plus a unit test for `enforcement_enabled()` across the mode and setting combinations.

### Frontend (Vitest)

Add Vitest and a config. Unit tests:

- `auth.ts`: set, get, clear round-trip; store updates.
- `api.ts`: attaches the bearer header when a token is set; on 401 clears the token and triggers the redirect. Mock `fetch`.

Full browser E2E with Playwright is out of scope for this change.

## README

Rewrite the Security section to cover the two layers, when control-plane auth is enforced (`auto` vs `always`), where the admin token comes from and how to log in, the `MCPE_ADMIN_TOKEN` break-glass var, and the recovery story when the minted token is lost.

## Migration and backward compatibility

- The `scope` column is added idempotently. Existing tokens become `proxy`.
- Existing local installs run in `auto` mode and are local, so enforcement stays off. No behavior change, zero-config preserved.
- The only behavior change is in `expose` mode, which is exactly where the hole was.

## Edge cases and failure modes

- Lost token in expose mode: the minted plaintext is shown once and only the hash is stored, so it cannot be recovered. Recovery is the `MCPE_ADMIN_TOKEN` break-glass var: set it, restart, log in, mint a fresh token via settings, then unset the var. Documented in the README.
- Concurrent mint: two requests both minting when none exists is possible but rare, since minting only happens when zero control tokens exist. Both tokens would be valid. Not worth locking.
- Disabling enforcement (`always` to `auto` in local, or `expose` to `local`): existing control tokens stay valid but unused. No cleanup needed.
- Dev mode: Vite proxies `/api`, so the token is attached the same way and the flow works.

## Out of scope

- Multi-user accounts, roles beyond `proxy` and `control`, password login.
- Session cookies and CSRF.
- Token expiry and rotation policy. Tokens match the existing model: no expiry, delete to revoke.
- Playwright E2E.

## Verification plan

- Backend tests green, including the new suite.
- Frontend Vitest green.
- Manual: boot in expose mode, confirm the admin token prints, log in through the SPA, confirm `/api` calls work, confirm the log stream works under auth, confirm a fresh local boot still needs no login.
