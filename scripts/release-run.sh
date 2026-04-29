#!/usr/bin/env bash
# One-shot launcher for a Mathodology release archive (Linux / macOS).
#
# Layout assumed (matches release.yml output):
#   ./gateway                         (binary, +x)
#   ./apps/web/dist/                  (prebuilt SPA, served at /)
#   ./apps/agent-worker/              (Python source; needs uv + Python 3.11+)
#   ./crates/gateway/migrations/      (sqlx migrations)
#   ./config/providers.toml           (LLM provider registry)
#   ./.env                            (created from .env.example on first run)
#
# What it does:
#   1. Loads .env (or copies from .env.example if missing).
#   2. Sets sane defaults (RUNS_DIR, STATIC_DIR, ports).
#   3. Calls scripts/preflight.sh if present (prints install hints on miss).
#   4. uv sync the worker venv (uses .uv-lock when present, falls back).
#   5. Starts gateway + worker; tails both; SIGINT shuts both down cleanly.
#
# Tunables: see .env.example. Override via env, e.g.
#   GATEWAY_PORT=9000 ./run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ---------- 1. .env loading ----------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    echo "!! .env missing — copying from .env.example. Edit it then re-run."
    cp .env.example .env
    echo "   At minimum, set one of DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY."
    exit 1
  else
    echo "!! .env.example also missing — corrupt archive?" >&2
    exit 1
  fi
fi

# Source .env. `set -a` exports everything; we strip carriage returns so
# Windows-edited files still work.
set -a
# shellcheck disable=SC1091
. <(tr -d '\r' < .env)
set +a

# ---------- 2. defaults ----------
: "${REDIS_URL:=redis://127.0.0.1:6379/0}"
: "${DATABASE_URL:=postgres://mm:mm@127.0.0.1:5432/mm}"
: "${GATEWAY_HOST:=127.0.0.1}"
: "${GATEWAY_PORT:=8080}"
: "${RUNS_DIR:=$ROOT/runs}"
: "${STATIC_DIR:=$ROOT/apps/web/dist}"
: "${DEV_AUTH_TOKEN:=dev-local-insecure-token}"
export REDIS_URL DATABASE_URL GATEWAY_HOST GATEWAY_PORT RUNS_DIR STATIC_DIR DEV_AUTH_TOKEN

# Worker reaches gateway on its loopback.
: "${GATEWAY_HTTP:=http://${GATEWAY_HOST}:${GATEWAY_PORT}}"
export GATEWAY_HTTP

mkdir -p "$RUNS_DIR"

# ---------- 3. preflight ----------
if [ -x "scripts/preflight.sh" ]; then
  if ! ./scripts/preflight.sh; then
    echo "!! preflight reported missing prerequisites (see above)."
    echo "   Run scripts/install.sh to install them automatically, or fix manually."
    exit 1
  fi
fi

# Best-effort connectivity probes (only if the CLI exists).
if command -v redis-cli >/dev/null 2>&1; then
  redis-cli -u "$REDIS_URL" PING >/dev/null 2>&1 \
    || { echo "!! Redis at $REDIS_URL unreachable. Start it (linux: 'sudo systemctl start redis-server'; macOS: 'brew services start redis')."; exit 1; }
fi
if command -v pg_isready >/dev/null 2>&1; then
  pg_isready -d "$DATABASE_URL" >/dev/null 2>&1 \
    || { echo "!! PostgreSQL at $DATABASE_URL not ready. Check 'pg_ctl status' / 'systemctl status postgresql'."; exit 1; }
fi

# ---------- 4. worker venv ----------
if ! command -v uv >/dev/null 2>&1; then
  echo "!! uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "==> uv sync (worker)"
( cd apps/agent-worker && (uv sync --frozen 2>/dev/null || uv sync) )

# ---------- 5. start ----------
GATEWAY_BIN="./gateway"
[ -x "$GATEWAY_BIN" ] || { echo "!! gateway binary not found or not +x at $GATEWAY_BIN"; exit 1; }

echo "==> starting gateway on http://$GATEWAY_HOST:$GATEWAY_PORT"
"$GATEWAY_BIN" &
GATEWAY_PID=$!

echo "==> starting worker"
( cd apps/agent-worker && uv run python -m agent_worker ) &
WORKER_PID=$!

cleanup() {
  trap - EXIT INT TERM
  echo
  echo "==> shutting down"
  kill -TERM "$GATEWAY_PID" "$WORKER_PID" 2>/dev/null || true
  # Give them 5s to exit gracefully, then SIGKILL.
  for _ in 1 2 3 4 5; do
    if ! kill -0 "$GATEWAY_PID" 2>/dev/null && ! kill -0 "$WORKER_PID" 2>/dev/null; then
      return
    fi
    sleep 1
  done
  kill -KILL "$GATEWAY_PID" "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cat <<EOF

Mathodology is up.
  UI       http://$GATEWAY_HOST:$GATEWAY_PORT/
  Gateway  http://$GATEWAY_HOST:$GATEWAY_PORT/health
  Auth     Bearer $DEV_AUTH_TOKEN  (dev token; CHANGE in production)

Press Ctrl-C to stop.
EOF

# Wait on both; if either dies, exit and trigger cleanup.
wait -n "$GATEWAY_PID" "$WORKER_PID" 2>/dev/null || true
EXIT_CODE=$?
echo "!! one of the services exited (code $EXIT_CODE), tearing down the other"
exit "$EXIT_CODE"
