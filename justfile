set shell := ["bash", "-cu"]
set dotenv-load := true

default:
    @just --list

# ---- bootstrap ----
bootstrap:
    test -f .env || cp .env.example .env
    cargo fetch
    uv sync
    pnpm install

# ---- infra ----
infra-up:
    docker compose up -d redis postgres

infra-down:
    docker compose down

# ---- dev ----
dev:
    overmind start -f Procfile.dev

dev-gateway:
    cargo run -p gateway

dev-worker:
    cd apps/agent-worker && uv run arq agent_worker.main.WorkerSettings

dev-web:
    pnpm --filter web dev

# ---- migrations ----
migrate:
    cd crates/gateway && sqlx migrate run

migrate-add name:
    cd crates/gateway && sqlx migrate add {{name}}

# ---- codegen ----
gen: gen-py gen-ts

gen-py:
    uv run datamodel-codegen \
        --input packages/contracts/openapi.yaml \
        --input-file-type openapi \
        --output packages/py-contracts/src/mm_contracts/generated.py \
        --output-model-type pydantic_v2.BaseModel \
        --target-python-version 3.11

gen-ts:
    pnpm exec openapi-typescript packages/contracts/openapi.yaml -o packages/ts-contracts/src/generated.ts

# ---- quality ----
fmt:
    cargo fmt --all
    uv run ruff format .
    pnpm -r exec prettier --write "src/**/*.{ts,vue,css}" || true

lint:
    cargo clippy --all-targets -- -D warnings
    uv run ruff check .
    pnpm -r run lint || true

test:
    cargo test --workspace
    uv run pytest apps/agent-worker -q || true
    pnpm -r run test -- --run || true

smoke:
    bash scripts/smoke_e2e.sh

clean:
    cargo clean
    rm -rf .venv **/__pycache__ **/node_modules **/dist
