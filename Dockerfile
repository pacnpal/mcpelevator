# mcpelevator — single image: control plane + reverse proxy + SPA, batteries-included.
# Stage 1: build the SvelteKit SPA.
FROM node:22-bookworm-slim AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # -> /fe/build (adapter-static, SPA fallback)

# uv + uvx binaries come from the official image — declared as a named stage so the
# COPY --from below references an alias (Hadolint DL3022) rather than an external ref.
FROM ghcr.io/astral-sh/uv:0.11.24 AS uv

# Stage 2: runtime — Python control plane + Node/npx + uv/uvx so npx/uvx MCP
# servers run with zero local setup (batteries-included).
FROM python:3.14-slim-bookworm AS runtime

# pipefail so a failure in the piped NodeSource setup below aborts the RUN (DL4006).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg tini git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
