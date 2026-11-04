# mcpelevator — single image: control plane + reverse proxy + SPA, batteries-included.
# Stage 1: build the SvelteKit SPA.
FROM node:26-bookworm-slim@sha256:bc0a2afd3b74ce12222fc8bab99271e66257f021078f5f4a5d687a77d280c1d9 AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # -> /fe/build (adapter-static, SPA fallback)

# uv + uvx binaries come from the official image — declared as a named stage so the
# COPY --from below references an alias (Hadolint DL3022) rather than an external ref.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# Stage 2: runtime — Python control plane + Node/npx + uv/uvx so npx/uvx MCP
# servers run with zero local setup (batteries-included).
FROM python:3.15-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd AS runtime

# pipefail so a failure in the piped NodeSource setup below aborts the RUN (DL4006).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Node 26 (Current line until it goes Active LTS in Oct 2026) is pinned deliberately
# to match the frontend build stage above — runtime npx and build-time Node stay on
# one major. Bump both together when moving to the next line.
# openssh-client: git's ssh transport for source-repository servers (`ssh://…`,
# `git@host:path`); --no-install-recommends would otherwise drop it from git's deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg tini git openssh-client \
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    # Refresh npm to a build whose bundled deps carry the fixes Trivy flags in the
    # version Node ships: tar >=7.5.21, brace-expansion >=5.0.12,
    # ip-address >=10.3.1.
    && npm install -g npm@11.21.1 \
    # npm 11.21.1 still bundles undici 6.29.0; CVE-2026-87018/-109383/-109869 are
    # fixed in 6.29.1. Its only consumer is npm's node-gyp (range ^6.25.0) and it has
    # no deps, so swap it in place (npm pack checks the registry integrity hash).
    # Drop this once an npm release bundles undici >=6.29.1.
    && npm_mods="$(npm root -g)/npm/node_modules" \
    && npm pack -q --pack-destination /tmp undici@6.29.1 \
    && rm -rf "${npm_mods}/undici" && mkdir "${npm_mods}/undici" \
    && tar xzf /tmp/undici-6.29.1.tgz -C "${npm_mods}/undici" --strip-components=1 \
    && rm /tmp/undici-6.29.1.tgz \
    && npm cache clean --force \
    # Remove the base image's system pip: nothing here uses it (the app runs from a
    # uv venv, servers install via uv/uvx), and its vendored msgpack, urllib3 and
    # pkg_resources (setuptools) carry HIGH CVEs no pip release has picked up yet.
    && python -m pip uninstall -y -q pip \
    && rm -rf /var/lib/apt/lists/*

# Common native build toolchain, baked in (issue #81): plenty of npx/uvx MCP servers
# compile native extensions on install (node-gyp, cmake-based wheels, Go/Rust builds)
# and expect these to exist. Debian's go/rust are the distro-pinned versions; a server
# that needs a newer toolchain can add it at startup via MCPE_APT_PACKAGES (see
# docker-entrypoint.sh) or a derived image. Kept as its own early layer so it caches
# independently of the app/dependency layers below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config golang rustc cargo \
    && rm -rf /var/lib/apt/lists/*

# uv + uvx (Python MCP servers) from the official image
COPY --from=uv /uv /uvx /bin/

WORKDIR /app/backend
# Dependency layer (cached): lockfile-driven for determinism.
COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# App code + built SPA
COPY backend/ ./
COPY --from=frontend /fe/build /app/frontend/build

# Derive the running version from the GitHub release tag: CI passes the tag as APP_VERSION
# (see .github/workflows/docker-image.yml), and app.__version__ reads MCPE_VERSION first (see
# backend/app/__init__.py). A non-release/local build gets the honest 0.0.0-dev default rather
# than a fake number. No source stamping — the env var is the single injection point.
ARG APP_VERSION=0.0.0-dev

# docker CLI ONLY (docker-ce-cli, not the daemon) from Docker's official apt repo, so the
# opt-in `docker` runner can launch image-packaged MCP servers against a mounted daemon —
# either the host's socket (sibling containers) or an isolated dind sidecar via DOCKER_HOST.
# CLI only: the daemon is never run in-image. The runner stays root-equivalent and disabled
# by default (see the docker_runner setting), so shipping the CLI is inert until enabled.
#
# Deliberately installed here, AFTER the cached dependency layers above, and keyed on
# APP_VERSION (referenced in the RUN, so the arg's value is part of this layer's cache key).
# Every release build carries a fresh APP_VERSION, so this layer always re-runs and reinstalls
# the LATEST published docker-ce-cli .deb — picking up a Go-patched rebuild the moment Docker
# ships one — without invalidating the expensive uv-sync/arm64 layers before it. That is what
# lets the Go stdlib CVEs suppressed in .trivyignore.yaml clear automatically on the next
# release once a fixed .deb exists, instead of a durably-cached layer re-shipping a
# now-fixable binary while the suppression hides it.
RUN echo "docker-ce-cli refresh for mcpelevator ${APP_VERSION}" \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && arch="$(dpkg --print-architecture)" \
    && echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/backend/.venv/bin:${PATH}" \
    MCPE_VERSION=${APP_VERSION} \
    MCPE_HOST=0.0.0.0 \
    MCPE_PORT=8080 \
    MCPE_DATA_DIR=/data \
    MCPE_FRONTEND_DIR=/app/frontend/build \
    npm_config_cache=/data/.npm-cache \
    UV_CACHE_DIR=/data/.uv-cache

VOLUME ["/data"]
EXPOSE 8080

# Entrypoint wrapper: installs MCPE_APT_PACKAGES (extra Debian packages beyond the
# baked toolchain above) at startup, then execs the CMD. Install failures warn and
# boot continues. tini stays PID 1 and the script exec's, so uvicorn remains tini's
# direct child and the bridge/npx/uvx subprocess trees are still reaped (no zombies).
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
# Bind where MCPE_HOST/MCPE_PORT say, not a hardcoded 8080: under host networking
# (the recommended Unraid/NAS setup) there is no port mapping, so the env var is
# the only way to move the port. `exec` keeps uvicorn as tini's direct child.
CMD ["/bin/sh", "-c", "exec uvicorn app.main:app --host \"${MCPE_HOST:-0.0.0.0}\" --port \"${MCPE_PORT:-8080}\""]
