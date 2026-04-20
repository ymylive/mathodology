#!/usr/bin/env bash
# Regenerate Python + TS types from OpenAPI.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Pydantic (py-contracts/generated.py)"
uv run datamodel-codegen \
  --input packages/contracts/openapi.yaml \
  --input-file-type openapi \
  --output packages/py-contracts/src/mm_contracts/generated.py \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.11

echo "==> TypeScript (ts-contracts/generated.ts)"
pnpm exec openapi-typescript packages/contracts/openapi.yaml \
  -o packages/ts-contracts/src/generated.ts

echo "==> done"
