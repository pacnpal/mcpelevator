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

# shots.yml targets http://localhost:8080. If the port is overridden here the
# shots would still scrape :8080, so warn rather than fail silently.
if [ "${PORT}" != "8080" ]; then
  echo "WARNING: MCPE_SCREENSHOT_PORT=${PORT}, but shots.yml targets port 8080." >&2
  echo "         Update shots.yml to match or the capture will hit the wrong server." >&2
fi

# Refuse to start if something already serves the port. On a repeat local run the
# old backend would keep answering health checks while our new uvicorn fails to
# bind, and we'd seed the stale process's database instead.
if curl -fsS "${BASE}/api/health" >/dev/null 2>&1; then
  echo "ERROR: something already serves ${BASE} — stop the leftover backend first." >&2
  exit 1
fi

DATA_DIR="$(mktemp -d)"
export MCPE_DATA_DIR="$DATA_DIR"

echo "==> Starting backend on ${BASE} (data dir: ${DATA_DIR})"
# No subshell: back-grounding inside one hides the job from this shell so disown
# can't detach it. nohup keeps the server alive past this step (each Actions
# `run:` is its own shell); disown drops it from the job table so the shell won't
# signal it on exit. cwd must be backend/ (frontend_dir is ../frontend/build).
pushd backend >/dev/null
MCPE_RESTART_BUDGET=1 nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
  >/tmp/mcpe-backend.log 2>&1 &
BACKEND_PID=$!
disown "${BACKEND_PID}"
popd >/dev/null
echo "${BACKEND_PID}" >/tmp/mcpe-backend.pid

echo "==> Waiting for the control plane to answer"
for _ in $(seq 1 60); do
  if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
    echo "backend exited before answering:" >&2; cat /tmp/mcpe-backend.log >&2; exit 1
  fi
  if curl -fsS "${BASE}/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "${BASE}/api/health" >/dev/null || { echo "backend never came up" >&2; cat /tmp/mcpe-backend.log >&2; exit 1; }

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
states=""
for _ in $(seq 1 90); do
  states="$(curl -fsS "${BASE}/api/servers" \
    | python3 -c 'import sys,json; print(" ".join(s["slug"]+"="+s["state"] for s in json.load(sys.stdin)))')"
  echo "    ${states:-<none>}"
  case " ${states} " in
    *"=starting "*|*"=stopping "*) sleep 2 ;;
    *) break ;;
  esac
done

# The README captions describe an exact mix ("2 of 5 running": Memory + Time up,
# Filesystem + Upstream Weather failed, Sequential Thinking stopped). Fail loudly
# rather than let the workflow commit a dashboard that no longer matches — e.g. a
# broken package, or the settle loop timing out with a server still "starting".
expect() {
  case " ${states} " in
    *" $1=$2 "*) ;;
    *) echo "ERROR: expected ${1}=${2}, got: ${states}" >&2; exit 1 ;;
  esac
}
expect memory running
expect time running
expect filesystem failed
expect sequential-thinking stopped
expect upstream-weather failed

echo "==> Backend ready at ${BASE} (states: ${states})"
