# Mathodology · MathModelAgent-Pro

![ci](https://github.com/ymylive/mathodology/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-blue)

Production-grade AI agent system for mathematical modeling competitions (MCM / ICM / CUMCM / 华数杯).

**Current phase**: Phase 1 MVP — 4-agent linear pipeline (Analyzer → Modeler → Coder → Writer) with local Jupyter execution, multi-protocol LLM gateway, and streaming frontend.

## Stack

- **Gateway** (Rust, axum) — HTTP/WebSocket front layer, LLM multi-protocol proxy (OpenAI-compat / Anthropic / Ollama / vLLM), cost accounting, Redis cache.
- **Agent worker** (Python) — 4-agent pipeline, Jupyter kernel orchestration. Never calls LLM SDKs directly; only the gateway.
- **Web** (Vue 3.5 + Vite + TS + Pinia + Tailwind 4) — streaming run view.
- **Infra** — Redis (Streams job queue + event bus), Postgres (run ledger), docker-compose for local dev.

## Architecture

Single repo with three toolchain workspaces coexisting at root: Cargo, uv, pnpm.

```
math_agent/
├── apps/
│   ├── agent-worker/       # Python worker: 4-agent pipeline + Jupyter kernel
│   └── web/                # Vue 3 SPA: run submission + streamed events
├── crates/
│   └── gateway/            # Rust axum server: LLM proxy, runs API, SSE/WS fan-out
├── packages/
│   ├── contracts/          # OpenAPI + event JSON schema (source of truth)
│   ├── py-contracts/       # Python codegen target (pydantic v2)
│   └── ts-contracts/       # TypeScript codegen target
├── config/                 # runtime config (providers.toml)
├── scripts/                # bootstrap + smoke_e2e.sh
├── justfile                # task runner
└── Procfile.dev            # overmind dev orchestration
```

## Developer setup (fresh clone)

macOS:

```bash
brew install postgresql@16 redis
pg_ctl -D /opt/homebrew/var/postgresql@16 -l /tmp/pg.log start
redis-server --daemonize yes
createuser -s mm && createdb mm -O mm   # run once

cp .env.example .env                    # then edit API keys if you want live LLM
just bootstrap                          # installs all three toolchains + deps
just migrate                            # sqlx migrations
just dev                                # gateway + worker + web (needs overmind)
```

Linux: replace the `brew` line with your package manager, and adjust the
`pg_ctl` data directory. Or use `just infra-up` for docker-compose Redis +
Postgres instead of local services.

Open http://127.0.0.1:5173 → enter a problem → click **Run** → watch streamed events.

## Testing

```bash
just lint     # cargo clippy + ruff + vue-tsc
just test     # cargo test + pytest + vitest
just smoke    # end-to-end ping through the live stack
```

CI runs the equivalent on every PR — see `.github/workflows/ci.yml`.

## Milestones

Phase 1 ships across M1–M8. Commit hashes for each milestone below:

| Milestone | Commit | Summary |
| --------- | ------ | ------- |
| M1 | `97c1609` | Monorepo skeleton, hello-world ping |
| M2 | `926fc5f` | Gateway + Postgres persistence + run lifecycle |
| M3 | `f7aca8f` | Full LLM gateway + streaming UI |
| M4 | `8151668` | `BaseAgent` + Analyzer calling real LLM gateway |
| M5 | `98d3e1e` | Jupyter kernel + CoderAgent + figure artifact serving |
| M6 | `c578b66` | Modeler + Writer → full 4-agent pipeline |
| M7 | `91833ba` | shadcn-vue + KaTeX + shiki UI polish |
| M8 |  _(in progress)_ | Anthropic adapter + provider fallback + CI |

## Roadmap

- **Phase 1** MVP with 4-agent linear pipeline (current)
- **Phase 2** HMML knowledge base + RAG retrieval
- **Phase 3** Critic-in-the-loop self-refine
- **Phase 4** Productionization (cloud sandbox, LaTeX templates, multi-tenant)
- **Phase 5** Vision model, multi-language kernels, HITL

## Known limitations

- **LLM API keys are loaded from env.** Put your DeepSeek / Anthropic / OpenAI /
  Moonshot keys in `.env` (see `.env.example`) if you want to exercise live
  providers. Absent keys, pipelines will fail at the first real LLM call.
- **`RUNS_DIR` must be an absolute path** when the worker and gateway are
  launched from different working directories, otherwise figure URLs
  (`/runs/:id/figures/...`) won't resolve. The default `./runs` only works when
  both processes share cwd — which `just dev` (overmind) guarantees.
- **Rust 1.83 is pinned** via `rust-toolchain.toml`. Several crates in
  `Cargo.lock` are held to versions compatible with 1.83; bumping the toolchain
  is an explicit future task (see M8 commit history for context).
- **Contract codegen (`just gen`) requires network.** `datamodel-code-generator`
  is not in `uv.lock`, so it is installed ad-hoc. The `contracts-drift` CI job
  is therefore non-blocking (`continue-on-error: true`).

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Short version: feature branch → PR
to `main` → CI green → squash merge.

## License

MIT (core). Commercial modules may be relicensed later.
