.PHONY: dev-backend dev-frontend build test lock docker fmt

# Run the backend control plane with autoreload (http://127.0.0.1:8080).
dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8080

# Run the SvelteKit dev server with HMR (http://localhost:5173, proxies to :8080).
dev-frontend:
	cd frontend && npm run dev

# Build the SPA into frontend/build (served by FastAPI in prod).
build:
	cd frontend && npm ci && npm run build

# Backend test suite.
test:
	cd backend && uv run pytest -q

# Refresh the uv lockfile (determinism).
lock:
	cd backend && uv lock

# Build + run the whole thing in Docker.
docker:
	docker compose up --build
