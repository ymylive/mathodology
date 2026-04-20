#!/usr/bin/env bash
# First-run setup. Idempotent.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> ensuring .env"
[ -f .env ] || cp .env.example .env

echo "==> cargo fetch"
cargo fetch

echo "==> uv sync"
uv sync

echo "==> pnpm install"
pnpm install

echo "==> done. Next: just infra-up && just migrate && just dev"
