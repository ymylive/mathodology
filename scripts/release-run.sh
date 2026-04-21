#!/usr/bin/env bash
# One-shot bootstrap for a Mathodology release archive (Linux / macOS).
#
# Responsibilities:
#   1. Verify prereqs (Redis reachable, Postgres reachable, uv installed).
#   2. Sync the Python worker virtualenv via uv.
#   3. Run sqlx migrations against the configured DATABASE_URL.
#   4. Start the gateway + worker in the foreground (Ctrl-C stops both).
#
# Env overrides: anything in the neighboring .env file. See .env.example.
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
elif [ -f .env.example ]; then
  echo "!! .env missing; copying from .env.example (edit to add API keys)"
  cp .env.example .env
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${REDIS_URL:=redis://127.0.0.1:6379/0}"
: "${DATABASE_URL:=postgres://mm:mm@127.0.0.1:5432/mm}"
: "${GATEWAY_HOST:=127.0.0.1}"
: "${GATEWAY_PORT:=8080}"
: "${RUNS_DIR:=$PWD/runs}"
export REDIS_URL DATABASE_URL GATEWAY_HOST GATEWAY_PORT RUNS_DIR

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "!! required binary not found: $1"; exit 1; }
}

echo "==> checking prereqs"
need uv
if ! command -v psql >/dev/null 2>&1; then
  echo "!! psql not found — Postgres client tools recommended for migrations"
fi

# Redis ping.
if command -v redis-cli >/dev/null 2>&1; then
  redis-cli -u "$REDIS_URL" PING >/dev/null \
    || { echo "!! Redis at $REDIS_URL unreachable"; exit 1; }
fi

echo "==> syncing Python worker deps"
(cd apps/agent-worker && uv sync --frozen) \
  || (cd apps/agent-worker && uv sync)

echo "==> applying sqlx migrations (idempotent)"
if command -v sqlx >/dev/null 2>&1; then
  (cd crates/gateway && sqlx migrate run --database-url "$DATABASE_URL") || true
else
  echo "!! sqlx-cli not installed; migrations will run on first gateway startup if present"
fi

mkdir -p "$RUNS_DIR"

echo "==> starting gateway on $GATEWAY_HOST:$GATEWAY_PORT"
./gateway &
GATEWAY_PID=$!

echo "==> starting worker"
(cd apps/agent-worker && uv run python -m agent_worker) &
WORKER_PID=$!

cleanup() {
  echo
  echo "==> shutting down"
  kill "$GATEWAY_PID" "$WORKER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cat <<EOF

Mathodology stack is up.
  Gateway  http://$GATEWAY_HOST:$GATEWAY_PORT
  Health   http://$GATEWAY_HOST:$GATEWAY_PORT/health
  UI dist  apps/web/dist  (serve with any static host)

Ctrl-C to stop.
EOF

wait
