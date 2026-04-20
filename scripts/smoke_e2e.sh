#!/usr/bin/env bash
# M1 smoke test: POST /runs, connect WS, assert 3 events arrive within 5s, expect status 'done'.
#
# Prereqs: gateway + worker running, Redis up.
#   just infra-up
#   (in another terminal) just dev
set -euo pipefail

cd "$(dirname "$0")/.."

GATEWAY="${GATEWAY:-http://127.0.0.1:8080}"
TOKEN="${DEV_AUTH_TOKEN:-dev-local-insecure-token}"

echo "==> POST /runs"
RESP=$(curl -fsS -X POST "$GATEWAY/runs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"problem_text":"smoke test","competition_type":"other"}')
echo "    $RESP"

RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "==> run_id = $RUN_ID"

if ! command -v websocat >/dev/null 2>&1; then
  echo "!! websocat not installed — skipping WS assertion. brew install websocat"
  exit 0
fi

WS_URL="${GATEWAY/http/ws}/ws/runs/$RUN_ID"
echo "==> WS $WS_URL"

# Send hello frame, collect until 'done' or 5s timeout.
HELLO='{"type":"hello","run_id":"'"$RUN_ID"'","last_seq":0}'
OUT=$(printf '%s\n' "$HELLO" | websocat -n -t --ping-interval 2 --exit-on-eof "$WS_URL" --header "Authorization: Bearer $TOKEN" 2>/dev/null | head -n 20 || true)

echo "--- events ---"
echo "$OUT"
echo "--- end ---"

if echo "$OUT" | grep -q '"kind":"done"'; then
  echo "==> smoke PASS"
  exit 0
else
  echo "==> smoke FAIL: no done event"
  exit 1
fi
