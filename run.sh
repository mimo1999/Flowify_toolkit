#!/usr/bin/env bash
# Flowify — start backend (FastAPI) + frontend (Vite) together.
# Re-runs are idempotent: venv and node_modules are reused if already present.
#
# Usage:
#   ./run.sh                    # start both servers
#   ./run.sh --test             # start, smoke-test endpoints, then exit
#   BACKEND_PORT=9000 ./run.sh  # override ports

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
LOG_DIR="$ROOT/.run-logs"
mkdir -p "$LOG_DIR"

TEST_MODE=0
[[ "${1:-}" == "--test" ]] && TEST_MODE=1

# ---------- pick the right Python / venv activator -----------------------------
if [[ "${OS:-}" == "Windows_NT" ]] || [[ "$(uname -s 2>/dev/null)" =~ ^(MINGW|MSYS|CYGWIN) ]]; then
  VENV_BIN="Scripts"
  PY="${PYTHON:-python}"
else
  VENV_BIN="bin"
  PY="${PYTHON:-python3}"
fi

log()  { printf '\033[36m[run]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[run]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[31m[run]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- backend setup ------------------------------------------------------
setup_backend() {
  log "Backend: ensuring venv at $BACKEND_DIR/.venv"
  if [[ ! -d "$BACKEND_DIR/.venv" ]]; then
    "$PY" -m venv "$BACKEND_DIR/.venv" || fail "could not create venv with '$PY'"
  fi
  # shellcheck disable=SC1090
  source "$BACKEND_DIR/.venv/$VENV_BIN/activate"
  log "Backend: installing requirements (quiet)"
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r "$BACKEND_DIR/requirements.txt" >/dev/null
  deactivate
}

# ---------- frontend setup -----------------------------------------------------
setup_frontend() {
  command -v npm >/dev/null 2>&1 || fail "npm not found in PATH"
  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    log "Frontend: installing npm packages"
    (cd "$FRONTEND_DIR" && npm install --silent)
  else
    log "Frontend: node_modules already present"
  fi
}

# ---------- launchers ----------------------------------------------------------
start_backend() {
  log "Backend: starting on :$BACKEND_PORT"
  (
    cd "$BACKEND_DIR"
    # shellcheck disable=SC1090
    source ".venv/$VENV_BIN/activate"
    exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >"$LOG_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!
}

start_frontend() {
  log "Frontend: starting on :$FRONTEND_PORT"
  (
    cd "$FRONTEND_DIR"
    exec npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
  ) >"$LOG_DIR/frontend.log" 2>&1 &
  FRONTEND_PID=$!
}

cleanup() {
  log "Shutting down…"
  [[ -n "${BACKEND_PID:-}" ]]  && kill "$BACKEND_PID"  2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------- health check helper -----------------------------------------------
wait_for() {
  local url="$1" name="$2" tries=60
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS -o /dev/null --max-time 2 "$url"; then
      log "$name is up ($url)"
      return 0
    fi
    sleep 1
  done
  warn "$name did not become ready at $url"
  warn "Last 30 log lines:"
  tail -n 30 "$LOG_DIR/${name,,}.log" 2>/dev/null >&2 || true
  return 1
}

# ---------- run ----------------------------------------------------------------
setup_backend
setup_frontend
start_backend
start_frontend

wait_for "http://127.0.0.1:$BACKEND_PORT/"               "Backend"  || exit 1
wait_for "http://127.0.0.1:$FRONTEND_PORT/"              "Frontend" || exit 1

if [[ $TEST_MODE -eq 1 ]]; then
  log "Smoke-testing endpoints…"

  # 1. root
  curl -fsS "http://127.0.0.1:$BACKEND_PORT/" >/dev/null
  log "  GET /                 OK"

  # 2. ingest the Flowify backend itself (Python repo we know works)
  # Convert POSIX-style path to native Windows path on Git Bash so the Python
  # backend doesn't double-prepend the drive letter.
  if command -v cygpath >/dev/null 2>&1; then
    REPO_PATH_FOR_API=$(cygpath -w "$BACKEND_DIR" | sed 's/\\/\\\\/g')
  else
    REPO_PATH_FOR_API="$BACKEND_DIR"
  fi
  ING=$(curl -fsS -X POST "http://127.0.0.1:$BACKEND_PORT/ingest_repo" \
        -H "Content-Type: application/json" \
        -d "{\"repo_path\": \"$REPO_PATH_FOR_API\"}")
  GID=$(printf '%s' "$ING" | python -c 'import sys,json; print(json.load(sys.stdin)["graph_id"])')
  log "  POST /ingest_repo     OK (graph_id=$GID)"

  # 3. graph view at depth 1
  curl -fsS "http://127.0.0.1:$BACKEND_PORT/graph?graph_id=$GID&depth=1" >/dev/null
  log "  GET /graph            OK"

  # 4. query — verifies retrieval + explanation + that query_id is now returned
  QRES=$(curl -fsS -X POST "http://127.0.0.1:$BACKEND_PORT/query" \
         -H "Content-Type: application/json" \
         -d "{\"graph_id\": \"$GID\", \"query\": \"how does ingestion work\", \"depth\": 2}")
  QID=$(printf '%s' "$QRES" | python -c 'import sys,json; print(json.load(sys.stdin).get("query_id") or "")')
  [[ -n "$QID" ]] || fail "POST /query returned no query_id (regression)"
  log "  POST /query           OK (query_id=$QID)"

  # 5. feedback round-trip
  curl -fsS -X POST "http://127.0.0.1:$BACKEND_PORT/feedback" \
       -H "Content-Type: application/json" \
       -d "{\"graph_id\": \"$GID\", \"query_id\": \"$QID\", \"rating\": \"helpful\"}" >/dev/null
  log "  POST /feedback        OK"

  # 6. analytics
  curl -fsS "http://127.0.0.1:$BACKEND_PORT/analytics?graph_id=$GID" >/dev/null
  log "  GET /analytics        OK"

  log "All smoke tests passed."
  exit 0
fi

log "Backend:  http://127.0.0.1:$BACKEND_PORT"
log "Frontend: http://127.0.0.1:$FRONTEND_PORT"
log "Logs:     $LOG_DIR/{backend,frontend}.log"
log "Press Ctrl-C to stop."
wait
