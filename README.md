# Mathodology · MathModelAgent-Pro

Production-grade AI agent system for mathematical modeling competitions (MCM / ICM / CUMCM / 华数杯).

**Current phase**: Phase 1 MVP — 4-agent linear pipeline (Analyzer → Modeler → Coder → Writer) with local Jupyter execution, multi-protocol LLM gateway, and streaming frontend.

## Stack

- **Gateway** (Rust, axum) — HTTP/WebSocket front layer, LLM multi-protocol proxy (OpenAI-compat / Anthropic / Ollama / vLLM), cost accounting, Redis cache.
- **Agent worker** (Python, arq) — 4-agent pipeline, Jupyter kernel orchestration. Never calls LLM SDKs directly; only the gateway.
- **Web** (Vue 3.5 + Vite + TS + Pinia + Tailwind 4) — streaming run view.
- **Infra** — Redis (job queue + event bus), Postgres (run ledger), docker-compose for local dev.

## Monorepo

Single repo with three toolchain workspaces coexisting at root: Cargo, uv, pnpm.

```
apps/         # runnable deployables (web, agent-worker)
crates/       # Rust crates (gateway)
packages/     # shared libs (contracts OpenAPI, py-contracts, ts-contracts)
config/       # runtime config (providers.toml)
scripts/      # bootstrap / codegen / smoke test
```

## Quick start (M1)

```bash
just bootstrap      # install all three toolchains, copy .env, fetch deps
just infra-up       # docker-compose up redis + postgres
just migrate        # sqlx migrations
just dev            # overmind runs gateway + worker + web concurrently
```

Open http://127.0.0.1:5173 → enter a problem → click Run → see streamed events.

`just smoke` runs the end-to-end ping test.

## Roadmap

- **Phase 1** MVP with 4-agent linear pipeline (current)
- **Phase 2** HMML knowledge base + RAG retrieval
- **Phase 3** Critic-in-the-loop self-refine
- **Phase 4** Productionization (cloud sandbox, LaTeX templates, multi-tenant)
- **Phase 5** Vision model, multi-language kernels, HITL

See `/Users/cornna/.claude/plans/eager-humming-spark.md` for the authoritative plan.

## License

MIT (core). Commercial modules may be relicensed later.
