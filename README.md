# Mathodology · MathModelAgent-Pro

**English** · [简体中文](./README_zh.md)

![ci](https://github.com/ymylive/mathodology/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-blue)
![version](https://img.shields.io/badge/version-v0.3.0-black)
[![issues](https://img.shields.io/github/issues/ymylive/mathodology)](https://github.com/ymylive/mathodology/issues)

> 👉 **Active work**: [open issues](https://github.com/ymylive/mathodology/issues?q=is%3Aopen) · [project board](https://github.com/users/ymylive/projects/1) · [roadmap](#roadmap--milestones) — every task is tracked as an issue and labeled by phase, so contributors always see the current plan, not yesterday's.

Production-grade AI agent system for mathematical modeling competitions (MCM / ICM / CUMCM / 华数杯).

![demo](docs/demo.gif)

One problem statement → a full submission-ready paper (PDF / DOCX / TeX / Markdown) in ≈ 30 minutes. Five agents (Analyzer → Searcher → Modeler → Coder → Writer) drive a live Jupyter kernel, stream tokens through a Rust gateway, and produce a 15–20 page paper with 5+ award-grade figures (sensitivity tornado, Monte-Carlo box-plot, 2-D heatmap, convergence curve, residual diagnostic) that follows COMAP Outstanding / CUMCM 一等奖 conventions.

---

## Table of contents

- [Why Mathodology](#why-mathodology)
- [Features](#features)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Paper export — 4 formats × 3 templates](#paper-export--4-formats--3-templates)
- [Chart catalog — 20 canonical types](#chart-catalog--20-canonical-types)
- [Award-mode prompts](#award-mode-prompts)
- [Multi-engine web search (MCP)](#multi-engine-web-search-mcp)
- [Benchmarks](#benchmarks)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Developer setup](#developer-setup)
- [Testing](#testing)
- [Roadmap & milestones](#roadmap--milestones)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [License](#license)

---

## Why Mathodology

Math modeling competitions reward a narrow, non-obvious skill stack: restate an ambiguous problem crisply, pick a model that actually fits, run the solve with the right diagnostics, and write it up in the style judges skim-read. The 72-hour contest format compresses this into one very stressful weekend.

Off-the-shelf LLM chat interfaces don't cut it — they hallucinate citations, forget they already picked a model two turns ago, and produce walls of text without figures. Mathodology is built specifically for this workflow:

- **First-principles model derivation**, not boilerplate method lookups
- **Live Jupyter kernel execution** with reproducible numerical results
- **Figures are first-class** — every chart is saved, captioned, and linked by id
- **Award-mode prompts** internalize COMAP official tips + CUMCM 评阅规范 + 2016 MCM B judges' commentary
- **Multi-format export** — paper goes out as submission-ready PDF with real `\begin{figure}` blocks, not screenshots

---

## Features

- **5-agent linear pipeline** — Analyzer (scope + sub-questions + approaches) → Searcher (arXiv + web) → Modeler (ModelSpec) → Coder (Jupyter cells + figures) → Writer (structured paper draft)
- **3 competition paper templates** — `cumcm` (中文 ctexart · xelatex · Fandol fonts) · `huashu` (华数杯) · `mcm` (MCM/ICM English article)
- **4 export formats** — PDF via Tectonic · DOCX via Pandoc · raw LaTeX · Markdown
- **20 canonical chart types** injected into the Coder prompt — tornado, heatmap, Monte-Carlo box-plot, Pareto front, convergence, residual, QQ, ROC, confusion matrix, network graph, radar, contour, 3D surface, etc.
- **MCP web search** via [open-webSearch](https://github.com/Aas-ee/open-webSearch) — Bing · Baidu · DuckDuckGo · CSDN · Juejin · Brave · Exa · Startpage, no API keys needed
- **Robust LLM routing** — OpenAI-compatible / Anthropic / Ollama / vLLM multi-protocol gateway with automatic fallback, per-call cost accounting, transport-error retry with exponential backoff
- **Streaming everything** — tokens fan out via Redis Streams + WebSocket; frontend renders KaTeX math and shiki-highlighted code in real time
- **Deterministic figure pipeline** — Writer uses `[[FIG:<id>]]` placeholders; pipeline substitutes them against the Coder's registered `Figure` list so figure references never break
- **197 tests** — 45 gateway (Rust) + 197 worker (Python; catalog + MCP client + pipeline) + vue-tsc + vite build

---

## Quick start

```bash
# 1. Prerequisites (macOS; Linux: use your package manager)
brew install postgresql@16 redis tectonic pandoc node
pg_ctl -D /opt/homebrew/var/postgresql@16 -l /tmp/pg.log start
redis-server --daemonize yes
createuser -s mm && createdb mm -O mm                    # once
npm install -g open-websearch                            # for MCP web search

# 2. Clone + install
git clone https://github.com/ymylive/mathodology.git
cd mathodology
cp .env.example .env                                     # edit with at least one LLM key
just bootstrap                                           # cargo fetch + uv sync + pnpm install
just migrate                                             # sqlx migrations

# 3. Run
just dev                                                 # gateway :8080 + worker + web :5173

# 4. Open http://127.0.0.1:5173 → paste a problem → Run
```

The web UI is at `:5173`. A typical CUMCM-style problem run takes about 28 minutes end-to-end and costs around ¥2 on a mid-tier reasoning model.

---

## How it works

```
 ┌─────────┐  ProblemInput   ┌──────────┐   AgentEvent stream   ┌─────────┐
 │ Web UI  │ ──────────────▶ │ Gateway  │ ◀──────────────────── │ Worker  │
 │  (Vue)  │                 │  (Rust)  │ XADD mm:events:<run>  │(Python) │
 │         │ ◀── WS replay ─ │          │                       │         │
 └─────────┘                 └──────────┘                       └─────────┘
                                  │                                  │
                                  │ XADD mm:jobs                     │ spawns
                                  ▼                                  ▼
                            ┌──────────┐                   ┌──────────────┐
                            │  Redis   │                   │  Jupyter     │
                            │  Streams │                   │  kernel      │
                            └──────────┘                   └──────────────┘
```

### Pipeline event sequence (one run)

```
stage.start(analyzer) → token*N → cost → agent.output(AnalyzerOutput)  → stage.done
stage.start(searcher) → log(queries) → log(papers) → agent.output(SearchFindings) → stage.done
stage.start(modeler)  → token*N → cost → agent.output(ModelSpec)       → stage.done
stage.start(coder)    → token*N → cost → log("executing cell N")*7
                                      → kernel.stdout*M
                                      → agent.output(CoderOutput)      → stage.done
stage.start(writer)   → token*N → cost → agent.output(PaperDraft)      → stage.done
done(status=success, notebook_path, paper_path, meta_path)
```

### Agent responsibilities

| Agent | Job | Output contract |
|---|---|---|
| **Analyzer** | Restate the problem, enumerate sub-questions, list assumptions, propose 2-6 candidate approaches, identify data requirements | `AnalyzerOutput` |
| **Searcher** | Derive 4-5 focused queries, hit arXiv + (via MCP) Bing/Baidu/DuckDuckGo/CSDN/Juejin in parallel, dedupe by URL/DOI, LLM-synthesize key findings | `SearchFindings` (papers, key_findings, datasets_mentioned) |
| **Modeler** | Consult HMML library of canonical methods, produce ONE fully-specified modeling approach with first-principles-derived equations, variables with units, numbered algorithm outline, validation strategy with dimensional / boundary / baseline checks, sensitivity plan for ≥ 3 key parameters | `ModelSpec` |
| **Coder** | Iterate up to 7 turns in a persistent Jupyter kernel; each turn picks a chart type from the catalog, runs one focused cell, saves PNG+SVG figures, registers them in `figures_saved` | `CoderOutput` (cells, figures, notebook_path, summary) |
| **Writer** | Produce a 12-section paper — abstract with ≥ 2 numerical results, Problem Analysis, Assumptions with Justification+Impact, Symbol table, Model, Algorithm, Results, Sensitivity Analysis, Strengths & Weaknesses, Conclusion, References ≥ 15 — embedding figures via `[[FIG:<id>]]` placeholders | `PaperDraft` |

---

## Paper export — 4 formats × 3 templates

The gateway exposes `GET /runs/:run_id/export/:format?template=<t>` which produces:

| Format | Pipeline | Typical size |
|---|---|---|
| `pdf` | Tera template → LaTeX → Tectonic (xelatex) | 900 KB – 1.5 MB |
| `docx` | `paper.md` → Pandoc → .docx with embedded PNGs | 600-800 KB |
| `tex` | Rendered Tera template | 30-40 KB |
| `md` | Pre-substituted `paper.md` | 25-35 KB |

Templates (`crates/gateway/templates/`):

| Template | Class | Cover | CJK |
|---|---|---|---|
| `cumcm` | `ctexart` | 国赛摘要页 + 目录 | Fandol Song/Hei bundled in Tectonic |
| `huashu` | `ctexart` | 华数杯封面 + 页眉 | Fandol |
| `mcm` | `article` | Summary sheet with Team Control Number / Problem / Year | `ctex` fallback for accidental Chinese |

Frontend side, the `ExportPanel` component shows a template selector + 5 format buttons (PDF / DOCX / LaTeX / Markdown / Notebook) with error mapping:

- `404` — `论文还未生成` (run not finished)
- `503` — `服务器缺少 tectonic/pandoc` (binary not on PATH)
- `500` — displays ≤ 4 KB of the compile stderr

Error codes stay in the response body as a clean JSON `{ code, error }`. Dev token auth via `Authorization: Bearer <DEV_AUTH_TOKEN>` or `?token=...`.

---

## Chart catalog — 20 canonical types

`apps/agent-worker/src/agent_worker/chart_catalog.py` ships 20 vetted chart types the Coder picks from. Every entry has: id (slug), display name (中英双语), when-to-use, when-NOT-to-use, 2-3 typical pitfalls, and a runnable matplotlib template 17-32 lines long.

Grouped by purpose:

| Purpose | Types |
|---|---|
| **Distribution** | `histogram_kde`, `boxplot_grouped`, `violinplot` |
| **Correlation / sensitivity** | `heatmap_correlation`, `heatmap_sensitivity`, `tornado_sensitivity` |
| **Optimization landscape** | `contour_2d`, `surface_3d`, `pareto_front` |
| **Time series / trend** | `line_plot`, `line_with_ci`, `convergence_curve` |
| **Regression diagnostics** | `scatter_regression`, `residual_plot`, `qq_plot` |
| **Classification diagnostics** | `roc_curve`, `confusion_matrix` |
| **Category comparison** | `bar_grouped_stacked`, `radar_chart` |
| **Relational** | `network_graph` |

The Coder's prompt carries only a compact markdown index (~1 KB / 20 rows). Snippets stay in the module — the LLM doesn't read them; the humans + future maintainers do.

Helpers `styled_figure()`, `save_figure(fig, fig_id, caption, width)`, and `annotate_peak(ax, x, y, label)` are inlined into the Jupyter kernel bootstrap so they're globally available without imports.

---

## Award-mode prompts

Writer / Modeler / Coder prompts are rewritten around 19 actionable rules distilled from 16 authoritative sources — COMAP official MCM/ICM Procedures and Tips PDF, 2016 MCM Problem B judges' commentary, Pitt MCM Guide, CUMCM 赛区评阅工作规范, Tsinghua 清风数学建模 materials. See `memory/project_award_mode.md` for the full distillation.

Highlights:

### Writer (hard rules)

1. Abstract ≤ 1 page, **must include ≥ 2 concrete numerical results** in the first paragraph (e.g., "prediction error 3.2%", "fuel saving 17%"). No vague "significant improvement".
2. Four-element abstract order: context → method → results (with units) → conclusion. Forbidden opening: restating the problem.
3. **No school / student names / region anywhere.** Signatures in memo problems: `Sincerely, Team #<ID>`.
4. Assumptions as numbered triples `{Assumption, Justification, Impact}` — every assumption tied to literature / data / domain reasoning.
5. References ≥ 15, each inline-cited ≥ 1x. English sources preferred for MCM; GB/T 7714 for CUMCM.
6. **Mandatory sections**: Sensitivity Analysis · Strengths & Weaknesses (≥ 3 strengths, ≥ 2 weaknesses — honest limitations, not "time constraints").
7. Pipeline self-check at end: abstract ≤ 1 page + ≥ 2 numbers · no names · every sub-question answered · ≥ 15 references · Sensitivity + S&W present · every `[[FIG:xxx]]` discussed in prose · no filler phrases.

### Modeler

- Must propose **≥ 2 candidate models** with explicit selection criterion (AIC / BIC / CV error / robustness / interpretability for the decision-maker). No "effect was better".
- Prefer first-principles derivation from the problem's physics / economics / business logic. Escalating complexity (LP → MILP → DL) requires justifying why the simpler method is insufficient (Occam's razor guardrail).
- `validation_strategy` MUST include all three of: dimensional analysis, boundary / limit-case test, baseline comparison.
- Sensitivity plan: ≥ 3 key parameters × ±10% / ±20% + Monte Carlo with N ≥ 1000 when parameters are highly uncertain. Tornado plot + heatmap figures requested explicitly.

### Coder

- Chart captions must be **independently readable** — include variable names AND a key number (`"残差直方图（均值 0.01，标准差 0.08，N=200）"`).
- Honor the Modeler's sensitivity plan: produce the requested tornado / heatmap / Monte-Carlo figures across iterations.
- `np.random.seed(...)` fixed before any stochastic call; constants named at the top of the first cell. Every printed numerical result must be reproducible from the notebook alone.

Raise `MAX_ITERATIONS` to 7 lets the Coder spread analyses across turns — baseline, tornado, heatmap, Monte-Carlo, scan, convergence, diagnostic — instead of cramming all 5 figures into one cell.

---

## Multi-engine web search (MCP)

Searcher fans out across sources in parallel:

| Source | Transport | Typical hit rate | Auth |
|---|---|---|---|
| arXiv | HTTP Atom API | High for methods / English papers | none |
| Bing / DuckDuckGo / Brave / Exa / Startpage | MCP stdio → open-webSearch | Medium | none (scraping) |
| Baidu / CSDN / Juejin | MCP stdio → open-webSearch | High for Chinese competition context | none |

For CUMCM / 华数杯 (Chinese competitions), the Searcher auto-generates an extra Chinese methodology query to hit Baidu / CSDN / Juejin — which cover 国赛论文精选 blog posts and solution walkthroughs that arXiv misses entirely.

Graceful degradation paths:
- `OPEN_WEBSEARCH_DISABLED=1` → disable MCP, fall back to arXiv-only
- MCP subprocess spawn fails (Node missing, binary not in PATH) → log warning, return empty web results, arXiv continues
- Per-engine rate limit (captcha / 429) → that engine returns empty for the run, others continue
- Per-query timeout (30s) → drop that query, keep others

Shared `_stream_with_retry` helper covers transport errors (`httpx.RemoteProtocolError`, `ReadTimeout`, `ConnectError`, `PoolTimeout`) AND silent empty 200 OK responses with exponential backoff 5s / 15s / 30s across 4 attempts. Observed on `cornna/gpt-5.4` upstream; retry logic prevents the Writer / Coder from failing on random upstream hiccups.

---

## Benchmarks

Representative run (bus-scheduling CUMCM problem, `gpt-5.4` via cornna provider, reasoning=low):

| Stage | Duration | Notes |
|---|---|---|
| Analyzer | 132 s | 12 sub-questions, 5 proposed approaches |
| Searcher | 4 s | arXiv returned 0 (niche problem); MCP disabled in this run |
| Modeler | 312 s | 28 variables, 18 equations, 13-step algorithm, full sensitivity plan |
| Coder | 758 s | 7 cells, 5 figures (baseline / tornado / heatmap / MC box-plot / headway scan) |
| Writer | 501 s | 12 sections, 16 references, abstract with 4 numerical results |
| **Total** | **28 min 30 s** | **¥2.22** |

Exported paper: **17 pages · 921 KB PDF · 5 figures embedded · 10 image streams**. All section headings, equations (LaTeX), and references rendered in CUMCM template with Fandol CJK fonts via Tectonic.

Test suite baseline:
- `cargo test -p gateway` — 45 passed, 3 `#[ignore]` (tectonic/pandoc real-compile, opt-in via `-- --ignored`)
- `uv run pytest apps/agent-worker` — 197 passed (figure pipeline + chart catalog + MCP stdio client)
- `pnpm --filter web typecheck && build` — vue-tsc clean, bundle 171 KB gzip (shiki ~282 KB gzip lazy-loaded)

---

## Configuration

### `.env` (examples of relevant keys)

```bash
# Gateway
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8080
DEV_AUTH_TOKEN=dev-local-insecure-token

# Infra
REDIS_URL=redis://127.0.0.1:6379/0
DATABASE_URL=postgres://mm:mm@127.0.0.1:5432/mm
RUNS_DIR=/absolute/path/to/runs             # MUST be absolute

# LLM provider keys (fill in at least one)
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
MOONSHOT_API_KEY=
CORNNA_API_KEY=sk-...

# Web search (MCP)
OPEN_WEBSEARCH_CMD=open-websearch            # or absolute path
OPEN_WEBSEARCH_ENGINES=bing,duckduckgo,baidu,csdn,juejin
OPEN_WEBSEARCH_DISABLED=false                # set to true to skip web search entirely
```

### `config/providers.toml` (LLM router)

```toml
[[providers]]
name = "deepseek"
kind = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]
price_input_per_1m = 1.0                     # RMB per 1M tokens
price_output_per_1m = 2.0

[router]
default_model = "deepseek-chat"
fallback = ["deepseek-chat", "moonshot-v1-32k"]
```

Per-run model override via `ProblemInput.model_override` — e.g., force `claude-sonnet-4-6` for one particularly hard problem while the rest default to `deepseek-chat`.

---

## Architecture

### Repo layout

```
math_agent/
├── apps/
│   ├── agent-worker/              # Python worker
│   │   └── src/agent_worker/
│   │       ├── agents/            # analyzer / searcher / modeler / coder / writer
│   │       ├── prompts/           # v1.toml per agent
│   │       ├── tools/             # arxiv client + web_search_mcp stdio client
│   │       ├── kernel/            # Jupyter session manager (inline chart helpers)
│   │       ├── chart_catalog.py   # 20 canonical chart types
│   │       ├── _chart_helpers.py  # styled_figure / save_figure / annotate_peak
│   │       ├── pipeline.py        # 5-agent orchestration + paper.meta.json
│   │       └── main.py            # XREADGROUP consumer on mm:jobs
│   └── web/                       # Vue 3 SPA
│       └── src/
│           ├── views/             # Showcase / Dashboard / Workbench
│           ├── components/        # ExportPanel, PaperDraft, AgentOutputCard…
│           ├── stores/            # Pinia: run, settings
│           └── api/               # figures + export + runs clients
├── crates/
│   └── gateway/
│       ├── src/
│       │   ├── routes/            # runs · figures · export · llm · stats · ws_run
│       │   ├── llm/               # OpenAI-compat + Anthropic streaming adapters
│       │   ├── auth.rs            # dev-token middleware
│       │   ├── cost.rs            # per-call cost accounting
│       │   └── app.rs             # axum router wiring
│       ├── templates/             # cumcm / huashu / mcm .tex.tera
│       └── tests/                 # integration tests (including #[ignore] real-compile)
├── packages/
│   ├── contracts/                 # OpenAPI + event JSON schema (source of truth)
│   ├── py-contracts/              # pydantic v2 codegen target
│   └── ts-contracts/              # TypeScript codegen target
├── config/
│   └── providers.toml             # LLM router
├── docs/
│   ├── demo.gif                   # README hero
│   └── promo/                     # polished promo HTML + MP4 sources
├── scripts/                       # bootstrap + smoke_e2e.sh
├── justfile                       # task runner
└── Procfile.dev                   # overmind dev orchestration
```

### Data contracts

| Layer | Format | Generated from |
|---|---|---|
| Event envelope (WebSocket) | `AgentEvent { run_id, agent, kind, seq, ts, payload }` | `packages/contracts/events.schema.json` |
| LLM completion | OpenAI-compatible SSE | gateway translates from Anthropic / others |
| Agent I/O | Pydantic v2 (Python) + TS interface | `packages/py-contracts/` + `packages/ts-contracts/` |
| `paper.meta.json` | Structured paper + figure list with `[[FIG:]]` placeholders | Writer emits, gateway export consumes |

### Seq counter

One Redis `INCR mm:seq:<run_id>` per event, shared between gateway and worker. WS replay filters by payload.seq. XADD uses `*` (time-based id), seq is the authoritative monotonic per-run counter.

---

## Developer setup

macOS (full recipe):

```bash
brew install postgresql@16 redis tectonic pandoc node@25
pg_ctl -D /opt/homebrew/var/postgresql@16 -l /tmp/pg.log start
redis-server --daemonize yes
createuser -s mm && createdb mm -O mm                    # run once
npm install -g open-websearch                            # MCP search subprocess

cp .env.example .env                                     # fill in at least one LLM key
just bootstrap                                           # installs cargo + uv + pnpm deps
just migrate                                             # sqlx migrations
just dev                                                 # overmind: gateway + worker + web
```

Linux: replace `brew` with your package manager. Alternatively use `just infra-up` for docker-compose Redis + Postgres instead of local services. Tectonic and Pandoc are still required on host for PDF/DOCX export — they run as subprocesses from the Rust gateway.

First Tectonic run downloads ~200 MB of TeXLive bundle into `~/Library/Caches/Tectonic`. CI jobs should cache this directory.

---

## Testing

```bash
just lint        # cargo clippy + ruff + vue-tsc
just test        # cargo test + pytest + vitest
just smoke       # end-to-end ping through the live stack

# Subset runs:
cargo test -p gateway                                    # 45 passed + 3 #[ignore]
cargo test -p gateway --test export_paper -- --ignored   # real tectonic + pandoc compile
uv run pytest apps/agent-worker                          # 197 passed
uv run pytest apps/agent-worker -m mcp                   # real open-websearch subprocess (network)
pnpm --filter web typecheck && pnpm --filter web build
```

CI runs the equivalent on every PR — see `.github/workflows/ci.yml`.

---

## Roadmap & milestones

| Phase | Status | Summary |
|---|---|---|
| **Phase 1 — MVP** | ✅ Shipped (M1–M8) | 4-agent linear pipeline, gateway, streaming UI, local Jupyter |
| **Phase 2 — Knowledge base** | ✅ Shipped (M9–M11) | HMML method library + BM25 retrieval · Searcher agent · hybrid search |
| **Phase 3 — Award-grade output** | ✅ Shipped (v0.3.0) | Award-mode prompts · 20-type chart catalog · MAX_ITER=7 · transport retry |
| **Phase 3.5 — Export + MCP** | ✅ Shipped (v0.3.0) | 4 formats × 3 templates · Tectonic + Pandoc · open-webSearch MCP |
| **Phase 4 — Critic loop** | In progress | Per-agent critic reviewer · self-refine via critique-act-revise |
| **Phase 5 — Productionization** | Planned | E2B / Daytona cloud sandbox · multi-tenant JWT · usage metering |
| **Phase 6 — Vision + multi-lang** | Planned | GPT-4V for chart QA · R + MATLAB kernels · HITL review gates |

### Detailed milestone log

| Milestone | Commit | Summary |
|---|---|---|
| M1 | `97c1609` | Monorepo skeleton, hello-world ping |
| M2 | `926fc5f` | Gateway + Postgres persistence + run lifecycle |
| M3 | `f7aca8f` | Full LLM gateway + streaming UI |
| M4 | `8151668` | `BaseAgent` + Analyzer calling real LLM gateway |
| M5 | `98d3e1e` | Jupyter kernel + CoderAgent + figure artifact serving |
| M6 | `c578b66` | Modeler + Writer → full 4-agent pipeline |
| M7 | `91833ba` | shadcn-vue + KaTeX + shiki UI polish |
| M8 | `3d21ad0` | Anthropic adapter + provider fallback + CI |
| M9 | `7ec8e50` | HMML knowledge base + Modeler integration |
| M10 | `4ebf7b4` | Searcher agent + 5-agent pipeline |
| M11 | `b7e8b08` | Hybrid BM25 + vector retrieval via fastembed |
| v0.2.0 | `10a06c6` | Editorial UI rebuild + reasoning effort + long context |
| **v0.3.0** | `b18df8d` | Competition paper export + award-mode pipeline + MCP search |

---

## Known limitations

- **LLM API keys are loaded from env.** Put at least one of DeepSeek / Anthropic / OpenAI / Moonshot / cornna keys in `.env` (see `.env.example`). Absent keys, pipelines fail at the first real LLM call.
- **`RUNS_DIR` must be an absolute path** when the worker and gateway are launched from different working directories, otherwise figure URLs (`/runs/:id/figures/...`) won't resolve. The default `./runs` only works when both processes share cwd — which `just dev` (overmind) guarantees.
- **Tectonic + Pandoc required for PDF/DOCX export.** PDF/DOCX export returns HTTP 503 with a clear message if either binary is missing. TeX and Markdown export work without them.
- **open-webSearch is a scraper, not an API.** Rate limiting and captcha from Bing/Baidu are regular. Per-engine failures are isolated — others keep working. Set `OPEN_WEBSEARCH_DISABLED=1` to skip it entirely.
- **Rust 1.83 is pinned** via `rust-toolchain.toml`. Several crates in `Cargo.lock` are held to 1.83-compatible versions; bumping the toolchain is an explicit future task.
- **Contract codegen (`just gen`) requires network.** `datamodel-code-generator` is not in `uv.lock`; it is installed ad-hoc. The `contracts-drift` CI job is non-blocking.
- **Upstream LLM flakiness.** Some OpenAI-compat proxies occasionally return empty 200 OK responses mid-run. The `_stream_with_retry` helper (base.py) retries on this with 5s / 15s / 30s backoff, but a persistently unavailable provider will still cause the run to fail after 4 attempts.

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Short version: feature branch → PR to `main` → CI green → squash merge. All new agents / prompts / chart types should have corresponding unit tests; real-external-service tests go behind `#[ignore]` (Rust) or `@pytest.mark.slow|mcp` (Python).

---

## License

MIT (core). See [LICENSE](./LICENSE). Commercial-grade modules (specialized templates, enterprise HITL, multi-tenant billing) may be relicensed under a commercial license in Phase 5+.
