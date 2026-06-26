# mcpelevator — single image: control plane + reverse proxy + SPA, batteries-included.
# Stage 1: build the SvelteKit SPA.
FROM node:22-bookworm-slim AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # -> /fe/build (adapter-static, SPA fallback)

# Stage 2: runtime — Python control plane + Node/npx + uv/uvx so npx/uvx MCP
# servers run with zero local setup (batteries-included).
FROM python:3.13-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg tini git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv + uvx (Python MCP servers) from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/backend
# Dependency layer (cached): lockfile-driven for determinism.
COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# App code + built SPA
COPY backend/ ./
COPY --from=frontend /fe/build /app/frontend/build

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
