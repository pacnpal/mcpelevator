# mcpelevator — single image: control plane + reverse proxy + SPA, batteries-included.
# Stage 1: build the SvelteKit SPA.
FROM node:26-bookworm-slim AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # -> /fe/build (adapter-static, SPA fallback)

# uv + uvx binaries come from the official image — declared as a named stage so the
# COPY --from below references an alias (Hadolint DL3022) rather than an external ref.
FROM ghcr.io/astral-sh/uv:0.11.25 AS uv

# Stage 2: runtime — Python control plane + Node/npx + uv/uvx so npx/uvx MCP
# servers run with zero local setup (batteries-included).
FROM python:3.14-slim-bookworm AS runtime

# pipefail so a failure in the piped NodeSource setup below aborts the RUN (DL4006).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Node 26 (Current line until it goes Active LTS in Oct 2026) is pinned deliberately
# to match the frontend build stage above — runtime npx and build-time Node stay on
# one major. Bump both together when moving to the next line.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg tini git \
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    # Refresh npm to a build whose bundled deps carry the fixes Trivy flags in the
    # version Node ships (tar >=7.5.16, the patched brace-expansion via minimatch,
    # and ip-address via the socks proxy chain).
    && npm install -g npm@11.17.0 \
    && npm cache clean --force \
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

# Stamp the release version (passed by CI as APP_VERSION) into the package metadata.
ARG APP_VERSION=0.1.0
RUN sed -i "s/^__version__ = .*/__version__ = \"${APP_VERSION}\"/" app/__init__.py

ENV PATH="/app/backend/.venv/bin:${PATH}" \
    MCPE_HOST=0.0.0.0 \
    MCPE_PORT=8080 \
    MCPE_DATA_DIR=/data \
    MCPE_FRONTEND_DIR=/app/frontend/build \
    npm_config_cache=/data/.npm-cache \
    UV_CACHE_DIR=/data/.uv-cache

VOLUME ["/data"]
EXPOSE 8080

# tini as PID 1 reaps the bridge/npx/uvx subprocess trees (no zombies/orphans).
ENTRYPOINT ["tini", "--"]
# Bind where MCPE_HOST/MCPE_PORT say, not a hardcoded 8080: under host networking
# (the recommended Unraid/NAS setup) there is no port mapping, so the env var is
# the only way to move the port. `exec` keeps uvicorn as tini's direct child.
CMD ["/bin/sh", "-c", "exec uvicorn app.main:app --host \"${MCPE_HOST:-0.0.0.0}\" --port \"${MCPE_PORT:-8080}\""]
