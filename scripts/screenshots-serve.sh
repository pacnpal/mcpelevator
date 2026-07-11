#!/usr/bin/env bash
# Boot the backend (which serves the built SPA + the /api control plane) on
# 127.0.0.1:8080 and seed a demo set of servers, so shot-scraper can capture a
# populated UI instead of an empty first-run state.
#
# Used by .github/workflows/screenshots.yml. Also runnable locally to regenerate
# the screenshots by hand:
#
#   make build                                   # -> frontend/build
#   cd backend && uv sync && cd ..
#   bash scripts/screenshots-serve.sh            # backend up + demo servers seeded
#   shot-scraper multi shots.yml --retina        # writes docs/screenshots/*.png
#
# The backend is left running in the background on purpose (the caller takes the
# screenshots, then the job/VM tears it down). A throwaway SQLite dir is used so
# runs start from a clean slate and never touch the repo.
set -euo pipefail

PORT="${MCPE_SCREENSHOT_PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
DATA_DIR="$(mktemp -d)"
export MCPE_DATA_DIR="$DATA_DIR"

echo "==> Starting backend on ${BASE} (data dir: ${DATA_DIR})"
# nohup so the server survives this step exiting (each Actions `run:` is its own
# shell; the process must outlive it for the screenshot step that follows).
( cd backend && nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
    >/tmp/mcpe-backend.log 2>&1 & echo $! >/tmp/mcpe-backend.pid )
disown || true

echo "==> Waiting for the control plane to answer"
for _ in $(seq 1 60); do
  if curl -fsS "${BASE}/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "${BASE}/api/health" >/dev/null || { echo "backend never came up"; cat /tmp/mcpe-backend.log; exit 1; }

echo "==> Seeding demo servers"
seed() { curl -fsS -X POST "${BASE}/api/servers" -H 'content-type: application/json' -d "$1" >/dev/null; }
# A deliberate mix: two that start cleanly (running), one local + one remote that
# fail (bad path / unresolvable host -> Failed), and one left disabled (Stopped).
seed '{"name":"Memory","runner":"npx","command":"npx","args":["-y","@modelcontextprotocol/server-memory"],"enabled":true}'
seed '{"name":"Filesystem","runner":"npx","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/data"],"enabled":true}'
seed '{"name":"Time","runner":"uvx","command":"uvx","args":["mcp-server-time"],"enabled":true}'
seed '{"name":"Sequential Thinking","runner":"npx","command":"npx","args":["-y","@modelcontextprotocol/server-sequential-thinking"],"enabled":false}'
seed '{"name":"Upstream Weather","runner":"remote","command":"https://weather.example.com/mcp","args":["streamable-http"],"env":{"Authorization":"Bearer demo-token"},"enabled":true}'

echo "==> Waiting for the reconciler to settle (no server left starting/stopping)"
# npx/uvx cold-start downloads can take a while; the failing ones error fast.
for _ in $(seq 1 90); do
  states="$(curl -fsS "${BASE}/api/servers" \
    | python3 -c 'import sys,json; print(" ".join(s["state"] for s in json.load(sys.stdin)))')"
  echo "    states: ${states:-<none>}"
  case " ${states} " in
    *" starting "*|*" stopping "*) sleep 2 ;;
    *) break ;;
  esac
done

echo "==> Backend ready at ${BASE}"
