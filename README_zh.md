# Mathodology · MathModelAgent-Pro

[English](./README.md) · **简体中文**

![ci](https://github.com/ymylive/mathodology/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-blue)
![version](https://img.shields.io/badge/version-v0.3.0-black)

面向数学建模竞赛（MCM / ICM / CUMCM / 华数杯）的生产级 AI Agent 系统。

![demo](docs/demo.gif)

一道题 → 一份可提交的参赛论文（PDF / DOCX / TeX / Markdown）+ 约 30 分钟。五个 Agent（Analyzer → Searcher → Modeler → Coder → Writer）驱动实时 Jupyter kernel，通过 Rust 网关 stream token，最终产出 15–20 页论文，含 5+ 张获奖级图表（Tornado 灵敏度图 / Monte Carlo 箱线图 / 二维热图 / 收敛曲线 / 残差诊断），遵循 COMAP Outstanding / CUMCM 一等奖 的写作规范。

---

## 目录

- [为什么做 Mathodology](#为什么做-mathodology)
- [核心功能](#核心功能)
- [快速开始](#快速开始)
- [工作原理](#工作原理)
- [论文导出 — 4 格式 × 3 模板](#论文导出--4-格式--3-模板)
- [图表目录 — 20 类 canonical 图表](#图表目录--20-类-canonical-图表)
- [获奖级 prompt](#获奖级-prompt)
- [多引擎 web 搜索（MCP）](#多引擎-web-搜索mcp)
- [性能基准](#性能基准)
- [配置](#配置)
- [架构](#架构)
- [开发者搭建](#开发者搭建)
- [测试](#测试)
- [路线图与里程碑](#路线图与里程碑)
- [已知限制](#已知限制)
- [贡献](#贡献)
- [许可证](#许可证)

---

## 为什么做 Mathodology

数学建模竞赛奖励的是一组狭窄且非直觉的能力栈：把模糊的问题精准地重述、选一个真正契合的模型、跑出带正确诊断的求解、再用评委扫读时会青睐的风格写出来。72 小时的比赛把这一切压缩成一个非常紧张的周末。

通用 LLM 聊天界面撑不住这活——它们虚构文献、忘了两轮之前已经选过模型了、产出整墙的纯文字而没有图。Mathodology 专为这个工作流构建：

- **第一性原理推导模型**，不是查套路
- **活的 Jupyter kernel 执行**，数值结果可复现
- **图表是一等公民** — 每张图都保存、配 caption、按 id 引用
- **获奖级 prompt** 内置了 COMAP 官方 tips + CUMCM 评阅规范 + 2016 MCM B 题评委讲评
- **多格式导出** — 论文以可提交 PDF 形式输出，真的 `\begin{figure}` 块，不是截图

---

## 核心功能

- **5-agent 线性流水线** — Analyzer（问题 scoping + 子问题 + 候选方法）→ Searcher（arXiv + web）→ Modeler（ModelSpec）→ Coder（Jupyter cell + 图表）→ Writer（结构化论文稿）
- **3 套竞赛论文模板** — `cumcm`（中文 ctexart · xelatex · Fandol 字体）· `huashu`（华数杯）· `mcm`（MCM/ICM 英文 article）
- **4 种导出格式** — Tectonic 编译 PDF · Pandoc 生成 DOCX · 原 LaTeX · Markdown
- **20 类 canonical 图表** 注入 Coder prompt — tornado、heatmap、Monte Carlo 箱线图、Pareto 前沿、收敛曲线、残差、QQ、ROC、混淆矩阵、网络图、雷达图、等高线、3D 曲面等
- **MCP web 搜索** 通过 [open-webSearch](https://github.com/Aas-ee/open-webSearch) — Bing · Baidu · DuckDuckGo · CSDN · Juejin · Brave · Exa · Startpage，全部**无需 API key**
- **鲁棒的 LLM 路由** — OpenAI-compat / Anthropic / Ollama / vLLM 多协议网关、自动 fallback、逐次调用成本计账、指数退避的传输错误重试
- **全链路 streaming** — token 通过 Redis Streams + WebSocket 扇出；前端实时渲染 KaTeX 公式和 shiki 高亮代码
- **确定性图表流水线** — Writer 用 `[[FIG:<id>]]` 占位符，pipeline 对照 Coder 注册的 `Figure` 列表做替换，图引用永不会坏
- **197 个测试** — 45 gateway（Rust）+ 197 worker（Python；catalog + MCP client + pipeline）+ vue-tsc + vite build

---

## 快速开始

```bash
# 1. 依赖（macOS；Linux 用各自的包管理器）
brew install postgresql@16 redis tectonic pandoc node
pg_ctl -D /opt/homebrew/var/postgresql@16 -l /tmp/pg.log start
redis-server --daemonize yes
createuser -s mm && createdb mm -O mm                    # 只跑一次
npm install -g open-websearch                            # MCP web 搜索子进程

# 2. 克隆 + 装依赖
git clone https://github.com/ymylive/mathodology.git
cd mathodology
cp .env.example .env                                     # 至少填一个 LLM key
just bootstrap                                           # cargo fetch + uv sync + pnpm install
just migrate                                             # sqlx 数据库迁移

# 3. 跑
just dev                                                 # gateway :8080 + worker + web :5173

# 4. 打开 http://127.0.0.1:5173 → 粘贴题目 → Run
```

典型 CUMCM 风格的题目跑完大约 28 分钟，中端 reasoning 模型上约 ¥2 成本。

---

## 工作原理

```
 ┌─────────┐  ProblemInput   ┌──────────┐   AgentEvent 流       ┌─────────┐
 │ Web UI  │ ──────────────▶ │ 网关      │ ◀──────────────────── │ Worker  │
 │  (Vue)  │                 │  (Rust)  │ XADD mm:events:<run>  │(Python) │
 │         │ ◀── WS replay ─ │          │                       │         │
 └─────────┘                 └──────────┘                       └─────────┘
                                  │                                  │
                                  │ XADD mm:jobs                     │ 启动
                                  ▼                                  ▼
                            ┌──────────┐                   ┌──────────────┐
                            │  Redis   │                   │  Jupyter     │
                            │  Streams │                   │  kernel      │
                            └──────────┘                   └──────────────┘
```

### 一次 run 的事件序列

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

### 各 Agent 职责

| Agent | 工作 | 输出契约 |
|---|---|---|
| **Analyzer** | 重述问题、列子问题、提假设、产出 2-6 个候选建模方法、识别数据需求 | `AnalyzerOutput` |
| **Searcher** | 提炼 4-5 条聚焦 query，并行打 arXiv + 通过 MCP 打 Bing/Baidu/DuckDuckGo/CSDN/Juejin，按 URL/DOI 去重，LLM 合成 key findings | `SearchFindings`（papers, key_findings, datasets_mentioned）|
| **Modeler** | 查 HMML 方法库，产出一个完整规范的建模方法：first-principles 推导的方程、带单位的变量、编号的算法步骤、含量纲/边界/基线三件套的 validation strategy、≥ 3 个关键参数的灵敏度计划 | `ModelSpec` |
| **Coder** | 在持久 Jupyter kernel 里最多 7 轮迭代；每轮从目录选一个图表类型，跑一个聚焦的 cell，存 PNG+SVG 图片，登记到 `figures_saved` | `CoderOutput`（cells, figures, notebook_path, summary）|
| **Writer** | 产出 12 节论文 — 摘要含 ≥ 2 个数值结果、问题分析、假设+理由+影响三元、符号表、模型、算法、结果、灵敏度分析、优缺点、结论、参考文献 ≥ 15 条 — 通过 `[[FIG:<id>]]` 占位符引图 | `PaperDraft` |

---

## 论文导出 — 4 格式 × 3 模板

网关暴露 `GET /runs/:run_id/export/:format?template=<t>`，产出：

| 格式 | 流水线 | 典型大小 |
|---|---|---|
| `pdf` | Tera 模板 → LaTeX → Tectonic（xelatex）| 900 KB – 1.5 MB |
| `docx` | `paper.md` → Pandoc → 带嵌入 PNG 的 .docx | 600-800 KB |
| `tex` | Tera 模板渲染结果 | 30-40 KB |
| `md` | 占位符已替换的 `paper.md` | 25-35 KB |

模板（`crates/gateway/templates/`）：

| 模板 | 文档类 | 封面 | CJK 支持 |
|---|---|---|---|
| `cumcm` | `ctexart` | 国赛摘要页 + 目录 | Tectonic bundle 自带 Fandol 宋体/黑体 |
| `huashu` | `ctexart` | 华数杯封面 + 页眉 | Fandol |
| `mcm` | `article` | Summary sheet 含 Team Control Number / Problem / Year | `ctex` 包做 CJK 后备（防止不小心出现中文变豆腐）|

前端侧，`ExportPanel` 组件有模板选择器 + 5 个格式按钮（PDF / DOCX / LaTeX / Markdown / Notebook），错误映射：

- `404` — `论文还未生成`（run 还没跑完）
- `503` — `服务器缺少 tectonic/pandoc`（binary 不在 PATH）
- `500` — 显示 ≤ 4 KB 编译 stderr 摘要

错误码以清爽 JSON `{ code, error }` 在 response body 返回。Dev token 认证通过 `Authorization: Bearer <DEV_AUTH_TOKEN>` 或 `?token=...`。

---

## 图表目录 — 20 类 canonical 图表

`apps/agent-worker/src/agent_worker/chart_catalog.py` 提供 20 类 Coder 可选的图表类型。每条包含：id（slug）、中英双语显示名、适用场景、不适用场景、2-3 条典型坑、17-32 行可运行的 matplotlib 模板。

按用途分组：

| 用途 | 类型 |
|---|---|
| **分布** | `histogram_kde`, `boxplot_grouped`, `violinplot` |
| **相关性 / 灵敏度** | `heatmap_correlation`, `heatmap_sensitivity`, `tornado_sensitivity` |
| **优化地形** | `contour_2d`, `surface_3d`, `pareto_front` |
| **时序 / 趋势** | `line_plot`, `line_with_ci`, `convergence_curve` |
| **回归诊断** | `scatter_regression`, `residual_plot`, `qq_plot` |
| **分类诊断** | `roc_curve`, `confusion_matrix` |
| **类别对比** | `bar_grouped_stacked`, `radar_chart` |
| **关系** | `network_graph` |

Coder prompt 里只塞一个精简的 markdown 索引（约 1 KB / 20 行）。snippets 留在模块里 — LLM 不读它们，人类维护者和后续开发者读。

辅助函数 `styled_figure()`、`save_figure(fig, fig_id, caption, width)`、`annotate_peak(ax, x, y, label)` 内联到 Jupyter kernel bootstrap，user namespace 全局可用，无需 import。

---

## 获奖级 prompt

Writer / Modeler / Coder prompt 围绕从 16 个权威来源蒸馏的 19 条可执行规则重写：COMAP 官方 MCM/ICM Procedures and Tips PDF、2016 MCM B 题评委讲评（Catherine Roberts）、Pitt MCM Guide、CUMCM 赛区评阅工作规范、清风数学建模讲义。完整蒸馏见 `memory/project_award_mode.md`。

要点：

### Writer（硬规则）

1. 摘要 ≤ 1 页，**第一段必须含 ≥ 2 个具体数值结果**（如"预测误差 3.2%"、"燃料节省 17%"）。禁止"取得了较好效果"等无数值 filler。
2. 摘要按四要素顺序：问题背景 → 方法 → 数值结果（带单位）→ 结论与推广。禁止以题目重述开头。
3. **论文任何位置不得出现校名 / 成员姓名 / 地理区域。** 需要签名时统一 `Sincerely, Team #<ID>`。
4. 假设必须按 `{假设, 理由, 影响}` 三段式逐条编号 — 每条假设挂到文献 / 数据 / 领域推理。
5. 参考文献 ≥ 15 条，每条 inline 引用 ≥ 1 次。美赛优先英文文献；国赛用 GB/T 7714。
6. **必备章节**：灵敏度分析 · 优缺点（≥ 3 条优点，≥ 2 条缺点 — 诚实限制，禁用"时间紧张"作为唯一弱点）。
7. 交付前 Pipeline self-check：摘要 ≤ 1 页 + ≥ 2 个数字 · 无姓名 · 每个子问题都回答 · ≥ 15 条文献 · 含灵敏度 + 优缺点 · 每个 `[[FIG:xxx]]` 在正文被讨论 · 无 filler 句式。

### Modeler

- 必须提出 **≥ 2 个候选模型** + 显式选择判据（AIC / BIC / CV 误差 / 鲁棒性 / 业务可解释性）。禁用"效果较好"。
- 优先从问题物理 / 经济 / 业务逻辑推导 first-principles 模型。升级复杂度（LP → MILP → DL）必须说明简单方法为何不够（奥卡姆剃刀）。
- `validation_strategy` 必须三件套齐全：量纲一致性、边界 / 极限值、基线对比。
- 灵敏度计划：≥ 3 个关键参数 × ±10% / ±20%；参数不确定性大时追加 Monte Carlo N ≥ 1000。显式要求 Tornado 图 + heatmap。

### Coder

- 图表 caption 必须**独立可读** — 含变量名 + 关键数值（`"残差直方图（均值 0.01，标准差 0.08，N=200）"`）。
- 尊重 Modeler 的灵敏度计划：按请求跨多轮产出 tornado / heatmap / Monte Carlo 图。
- 每次 stochastic 调用前 `np.random.seed(...)` 固定；常量命名写在第一个 cell 顶部。所有打印的数值结果必须能通过 notebook 独立复现。

把 `MAX_ITERATIONS` 提到 7 允许 Coder 把分析分摊到多轮 — 基线、tornado、heatmap、Monte Carlo、参数扫描、收敛、诊断 — 而不是把 5 张图塞一个 cell 里。

---

## 多引擎 web 搜索（MCP）

Searcher 并行调用多源：

| 源 | 传输 | 典型命中率 | 认证 |
|---|---|---|---|
| arXiv | HTTP Atom API | 方法 / 英文论文命中高 | 无 |
| Bing / DuckDuckGo / Brave / Exa / Startpage | MCP stdio → open-webSearch | 中等 | 无（爬取）|
| Baidu / CSDN / Juejin | MCP stdio → open-webSearch | 中文竞赛内容命中高 | 无 |

对 CUMCM / 华数杯 等中文竞赛，Searcher 自动生成一条中文方法论 query 来命中 Baidu / CSDN / Juejin — 这些站点覆盖"国赛论文精选"博客、解题流程，arXiv 完全覆盖不到。

优雅降级路径：
- `OPEN_WEBSEARCH_DISABLED=1` → 禁用 MCP，退回纯 arXiv
- MCP 子进程 spawn 失败（Node 缺失、binary 不在 PATH）→ 打 warning log，返回空 web 结果，arXiv 继续
- 单引擎触发限流（captcha / 429）→ 该引擎当次返回空，其他引擎继续
- 单 query 超时（30s）→ 丢掉该 query，其他保留

共享的 `_stream_with_retry` helper 统一处理传输错误（`httpx.RemoteProtocolError`、`ReadTimeout`、`ConnectError`、`PoolTimeout`）**和** 静默空 200 OK 响应，以 5s / 15s / 30s 指数退避，最多 4 次尝试。观察到 `cornna/gpt-5.4` 上游偶发此类问题；retry 逻辑让 Writer / Coder 不因上游随机抽风而失败。

---

## 性能基准

代表性 run（公交调度 CUMCM 题，`gpt-5.4` via cornna，reasoning=low）：

| 阶段 | 时长 | 备注 |
|---|---|---|
| Analyzer | 132 s | 12 个子问题、5 个候选方法 |
| Searcher | 4 s | arXiv 返回 0（题目冷门）；本次 run 禁用了 MCP |
| Modeler | 312 s | 28 个变量、18 个方程、13 步算法、完整灵敏度计划 |
| Coder | 758 s | 7 个 cell、5 张图（基线 / tornado / heatmap / MC 箱线图 / 班距扫描）|
| Writer | 501 s | 12 节、16 条文献、摘要含 4 个数值结果 |
| **总计** | **28 分 30 秒** | **¥2.22** |

导出论文：**17 页 · 921 KB PDF · 5 张图嵌入 · 10 个 image stream**。所有章节标题、公式（LaTeX）、参考文献在 CUMCM 模板下用 Tectonic + Fandol CJK 字体正确渲染。

测试基线：
- `cargo test -p gateway` — 45 通过，3 个 `#[ignore]`（tectonic/pandoc 真实编译，通过 `-- --ignored` 开启）
- `uv run pytest apps/agent-worker` — 197 通过（figure pipeline + chart catalog + MCP stdio client）
- `pnpm --filter web typecheck && build` — vue-tsc 干净，主 bundle 171 KB gzip（shiki ~282 KB gzip 按需懒加载）

---

## 配置

### `.env`（相关 key 示例）

```bash
# 网关
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8080
DEV_AUTH_TOKEN=dev-local-insecure-token

# 基础设施
REDIS_URL=redis://127.0.0.1:6379/0
DATABASE_URL=postgres://mm:mm@127.0.0.1:5432/mm
RUNS_DIR=/absolute/path/to/runs             # 必须绝对路径

# LLM provider key（至少填一个）
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
MOONSHOT_API_KEY=
CORNNA_API_KEY=sk-...

# Web 搜索（MCP）
OPEN_WEBSEARCH_CMD=open-websearch            # 或绝对路径
OPEN_WEBSEARCH_ENGINES=bing,duckduckgo,baidu,csdn,juejin
OPEN_WEBSEARCH_DISABLED=false                # true 则完全跳过 web 搜索
```

### `config/providers.toml`（LLM 路由）

```toml
[[providers]]
name = "deepseek"
kind = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]
price_input_per_1m = 1.0                     # 每 1M token 的 RMB 价格
price_output_per_1m = 2.0

[router]
default_model = "deepseek-chat"
fallback = ["deepseek-chat", "moonshot-v1-32k"]
```

单次 run 的模型 override 通过 `ProblemInput.model_override` — 比如某个特别难的题目强制用 `claude-sonnet-4-6`，其他默认走 `deepseek-chat`。

---

## 架构

### 仓库布局

```
math_agent/
├── apps/
│   ├── agent-worker/              # Python worker
│   │   └── src/agent_worker/
│   │       ├── agents/            # analyzer / searcher / modeler / coder / writer
│   │       ├── prompts/           # 每个 agent 的 v1.toml
│   │       ├── tools/             # arxiv 客户端 + web_search_mcp stdio 客户端
│   │       ├── kernel/            # Jupyter 会话管理器（内联 chart helpers）
│   │       ├── chart_catalog.py   # 20 类 canonical 图表
│   │       ├── _chart_helpers.py  # styled_figure / save_figure / annotate_peak
│   │       ├── pipeline.py        # 5-agent 编排 + paper.meta.json
│   │       └── main.py            # mm:jobs 上的 XREADGROUP 消费者
│   └── web/                       # Vue 3 SPA
│       └── src/
│           ├── views/             # Showcase / Dashboard / Workbench
│           ├── components/        # ExportPanel, PaperDraft, AgentOutputCard…
│           ├── stores/            # Pinia: run, settings
│           └── api/               # figures + export + runs 客户端
├── crates/
│   └── gateway/
│       ├── src/
│       │   ├── routes/            # runs · figures · export · llm · stats · ws_run
│       │   ├── llm/               # OpenAI-compat + Anthropic 流式适配器
│       │   ├── auth.rs            # dev-token 中间件
│       │   ├── cost.rs            # 逐次调用成本累加
│       │   └── app.rs             # axum 路由装配
│       ├── templates/             # cumcm / huashu / mcm .tex.tera
│       └── tests/                 # 集成测试（含 #[ignore] 真编译）
├── packages/
│   ├── contracts/                 # OpenAPI + 事件 JSON schema（真源）
│   ├── py-contracts/              # pydantic v2 codegen 目标
│   └── ts-contracts/              # TypeScript codegen 目标
├── config/
│   └── providers.toml             # LLM 路由
├── docs/
│   ├── demo.gif                   # README hero
│   └── promo/                     # 精制 promo HTML + MP4 源
├── scripts/                       # bootstrap + smoke_e2e.sh
├── justfile                       # 任务运行器
└── Procfile.dev                   # overmind 开发编排
```

### 数据契约

| 层 | 格式 | 生成于 |
|---|---|---|
| 事件信封（WebSocket）| `AgentEvent { run_id, agent, kind, seq, ts, payload }` | `packages/contracts/events.schema.json` |
| LLM completion | OpenAI 兼容 SSE | 网关从 Anthropic / 其他协议翻译而来 |
| Agent I/O | Pydantic v2（Python）+ TS interface | `packages/py-contracts/` + `packages/ts-contracts/` |
| `paper.meta.json` | 含 `[[FIG:]]` 占位符的结构化论文 + 图列表 | Writer 产出，网关导出消费 |

### Seq 计数器

每个事件 `INCR mm:seq:<run_id>` 一次，网关和 worker 共享 Redis 的这个 key。WS replay 按 payload.seq 过滤。XADD 用 `*`（时间戳 id），seq 是权威的 per-run 单调计数器。

---

## 开发者搭建

macOS（完整流程）：

```bash
brew install postgresql@16 redis tectonic pandoc node@25
pg_ctl -D /opt/homebrew/var/postgresql@16 -l /tmp/pg.log start
redis-server --daemonize yes
createuser -s mm && createdb mm -O mm                    # 只跑一次
npm install -g open-websearch                            # MCP 搜索子进程

cp .env.example .env                                     # 至少填一个 LLM key
just bootstrap                                           # cargo + uv + pnpm 依赖全装
just migrate                                             # sqlx 迁移
just dev                                                 # overmind: gateway + worker + web
```

Linux：把 `brew` 换成你的包管理器。或者用 `just infra-up` 跑 docker-compose 的 Redis + Postgres 替代本地服务。Tectonic 和 Pandoc 仍要求装在宿主上 — 它们作为 subprocess 被 Rust 网关调用。

Tectonic 首次运行会下载 ~200 MB 的 TeXLive bundle 到 `~/Library/Caches/Tectonic`。CI job 应该缓存这个目录。

---

## 测试

```bash
just lint        # cargo clippy + ruff + vue-tsc
just test        # cargo test + pytest + vitest
just smoke       # 活动栈端到端 ping

# 子集：
cargo test -p gateway                                    # 45 通过 + 3 #[ignore]
cargo test -p gateway --test export_paper -- --ignored   # 真实 tectonic + pandoc 编译
uv run pytest apps/agent-worker                          # 197 通过
uv run pytest apps/agent-worker -m mcp                   # 真实 open-websearch 子进程（走网络）
pnpm --filter web typecheck && pnpm --filter web build
```

每个 PR 的 CI 都跑等价命令 — 见 `.github/workflows/ci.yml`。

---

## 路线图与里程碑

| 阶段 | 状态 | 概述 |
|---|---|---|
| **Phase 1 — MVP** | ✅ 已发（M1–M8）| 4-agent 线性流水线、网关、streaming UI、本地 Jupyter |
| **Phase 2 — 知识库** | ✅ 已发（M9–M11）| HMML 方法库 + BM25 检索 · Searcher agent · 混合搜索 |
| **Phase 3 — 获奖级产出** | ✅ 已发（v0.3.0）| 获奖 prompt · 20 类图表目录 · MAX_ITER=7 · 传输 retry |
| **Phase 3.5 — 导出 + MCP** | ✅ 已发（v0.3.0）| 4 格式 × 3 模板 · Tectonic + Pandoc · open-webSearch MCP |
| **Phase 4 — Critic loop** | 进行中 | 每个 agent 的 critic 审查 · critique-act-revise 自我精修 |
| **Phase 5 — 生产化** | 规划中 | E2B / Daytona 云沙箱 · 多租户 JWT · 用量计费 |
| **Phase 6 — 视觉 + 多语言** | 规划中 | GPT-4V 检查图表 · R + MATLAB kernel · HITL review gate |

### 详细里程碑日志

| Milestone | Commit | 概述 |
|---|---|---|
| M1 | `97c1609` | Monorepo 骨架、hello-world ping |
| M2 | `926fc5f` | 网关 + Postgres 持久化 + run 生命周期 |
| M3 | `f7aca8f` | 完整 LLM 网关 + streaming UI |
| M4 | `8151668` | `BaseAgent` + Analyzer 调真实 LLM 网关 |
| M5 | `98d3e1e` | Jupyter kernel + CoderAgent + 图片产物服务 |
| M6 | `c578b66` | Modeler + Writer → 完整 4-agent 流水线 |
| M7 | `91833ba` | shadcn-vue + KaTeX + shiki UI 打磨 |
| M8 | `3d21ad0` | Anthropic adapter + provider fallback + CI |
| M9 | `7ec8e50` | HMML 知识库 + Modeler 集成 |
| M10 | `4ebf7b4` | Searcher agent + 5-agent 流水线 |
| M11 | `b7e8b08` | 混合 BM25 + 向量检索 via fastembed |
| v0.2.0 | `10a06c6` | 编辑部风格 UI 重构 + reasoning effort + long context |
| **v0.3.0** | `b18df8d` | 竞赛论文导出 + 获奖 pipeline + MCP 搜索 |

---

## 已知限制

- **LLM API key 从环境变量读。** `.env` 里至少填一个 DeepSeek / Anthropic / OpenAI / Moonshot / cornna 的 key（参考 `.env.example`）。完全没 key 的话，pipeline 第一次真 LLM 调用就炸。
- **`RUNS_DIR` 必须是绝对路径** —— 当 worker 和网关从不同工作目录启动时，否则图片 URL（`/runs/:id/figures/...`）解析不到。默认 `./runs` 只在两个进程共享 cwd 的情况下能跑 —— `just dev`（overmind）保证了这一点。
- **Tectonic + Pandoc 是 PDF/DOCX 导出的前置条件。** 缺任一 binary，PDF/DOCX 导出返 HTTP 503 + 明确提示。TeX 和 Markdown 导出不依赖。
- **open-webSearch 是爬虫，不是 API。** Bing / Baidu 的限流和 captcha 是常态。单引擎失败被隔离，其他引擎继续工作。设 `OPEN_WEBSEARCH_DISABLED=1` 可完全跳过。
- **Rust 1.83 toolchain 被 pin 住** —— 通过 `rust-toolchain.toml`。`Cargo.lock` 里几个 crate 被固定到 1.83 兼容的版本；升级 toolchain 是个明确的未来任务。
- **契约 codegen（`just gen`）需要网络。** `datamodel-code-generator` 不在 `uv.lock`；它是临时装的。`contracts-drift` CI job 因此非阻塞。
- **上游 LLM 的 flakiness。** 某些 OpenAI-compat proxy 偶尔在一次 run 中途返回空 200 OK。`_stream_with_retry` helper（base.py）用 5s / 15s / 30s 退避做 retry，但持续不可用的 provider 仍然会在 4 次尝试后让 run 失败。

---

## 贡献

参见 [CONTRIBUTING.md](./CONTRIBUTING.md)。短版：feature branch → PR 到 `main` → CI 绿 → squash merge。新增 agent / prompt / 图表类型都应有对应的单元测试；依赖真实外部服务的测试挂到 `#[ignore]`（Rust）或 `@pytest.mark.slow|mcp`（Python）后面。

---

## 许可证

MIT（核心）。见 [LICENSE](./LICENSE)。商业化模块（专用模板、企业 HITL、多租户计费）可能在 Phase 5+ 阶段转为商业许可。
