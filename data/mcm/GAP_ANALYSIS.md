# Mathodology → MCM Finalist (F 奖) 差距分析

> 时间：2026-05-14。基于本仓 `apps/agent-worker` + `crates/gateway` 当前 main (HEAD `423ca43`)。
> **F 奖定位说明**：MCM 奖项金字塔从高到低为 Outstanding Winner / Finalist / Meritorious / Honorable Mention / Successful Participant。**Finalist (F 奖) ≈ Top 1-2%**，比 Meritorious 高一档，比 Outstanding 低一档。

## TL;DR

项目已经具备 **Outstanding-level prompt 规则** 和 **Critic 多角色复评循环**，整体骨架接近 Finalist 要求。**关键差距集中在「证据厚度」而非「框架」**：缺真实数据接入、缺得奖论文 few-shot、缺 LaTeX 编译保险、缺人工审阅闭环、单线程缺备选模型回退。下表是按 ROI 排的差距清单。

## 现状（已具备 ✅）

| 维度 | 现状 |
|---|---|
| Pipeline | Analyzer → Searcher (arXiv+OpenAlex+Crossref+Tavily+open-webSearch) → Modeler → Coder (Jupyter, 7-turn) → Writer，5 stage + Critic loop |
| Critic | 3 角色（modeling_coach / academic_reviewer / code_reviewer），分级 blocking/major/minor，2 轮 revision，预算保护 |
| Writer prompt | 8 条 self-check、≤1 页摘要必含 ≥2 数值、≥15 ref、Sensitivity + Strengths/Weaknesses 强制 |
| Modeler prompt | ≥2 候选模型 + 显式判据、量纲/极限/baseline 三检查、≥3 参数 ±10/20% + Monte Carlo |
| Coder prompt | 20 类 chart catalog、styled_figure helper、5-8 图目标、`np.random.seed` 强制 |
| 知识库 | HMML 31 个方法 (BM25)，按 9 类目录组织 |
| 检索 | arXiv + OpenAlex + Crossref + Tavily + open-webSearch (Baidu/CSDN/Juejin)，top-3 PDF 全文注入 |
| 多语言 | MCM/ICM 英文 + CUMCM/华数杯 中文双轨 |
| 导出 | paper.md → PDF 流水线（tectonic + pandoc，12 个 format×competition 组合通过） |

## 差距清单（按 ROI 排序）

### 🔴 P0 — 直接决定能否进 F 奖名单（核心证据厚度）

| # | 差距 | 后果 | 建议 |
|---|---|---|---|
| 1 | **没有真实数据接入** | MCM 现代题 (2018+) 普遍带 CSV/Excel/Image 附件；现在 Coder 只能"模拟数据"，判分被打骨折 | 加一个 **DataLoader agent**：解析附件→pandas DataFrame→schema 报告，喂给 Coder。能跟 Analyzer 的 `data_requirements` 闭环 |
| 2 | **没有得奖论文 few-shot** | Prompt 全是抽象规则，LLM 缺"长这样才叫一等奖"的具象锚点；中等模型生成结果偏 Meritorious | 把刚下载的 dick20 (~150+ 篇 Outstanding/Finalist) 切片：抽 abstract、目录、sensitivity 章节做 BM25/向量库，Writer/Modeler prompt 注入 top-3 同题型示例 |
| 3 | **Coder 7-turn 硬上限 + 单 Jupyter** | F 奖论文常有 8-15 图 + 二级模型；7 turn 不够，大数据集本地 Jupyter OOM | turn 上限按问题难度自适应 (5-12)；接 E2B/Daytona 云端 sandbox（roadmap 已列 Phase 4） |
| 4 | **没有 PDF compile 早期失败检测** | tectonic 编译失败 ≠ 论文失败，但流水线只在最后一步 compile，错了无法回滚 | Writer 输出后立刻 dry-compile（quick mode），失败把错误回给 Writer 做 revision；F 奖必须有可读 PDF |
| 5 | **Sensitivity 是建议、不是验收门** | Critic 会查"Sensitivity 章节存在"，不查"参数 ±20% 时目标变化率被报告" | Critic 加 evidence-mining：抓 `tornado_sensitivity` 图 + 章节内 "%" 数字、不达 ≥3 个参数就 blocking |

### 🟠 P1 — 拉开 F 奖与 M 奖距离（说服力 + 完整性）

| # | 差距 | 后果 | 建议 |
|---|---|---|---|
| 6 | **Modeler 只锁一个 chosen_approach** | F 奖论文常做 Model I/II 对比 (Pareto on 复杂度 vs 精度)；现在只比"在 rationale 里口头比" | Modeler 可选 dual-track：return `chosen_approach` + `baseline_approach`，Coder 同时跑两份，Writer 在 Results 章节对比 |
| 7 | **缺人工/模型审阅"模拟判官"** | Critic 3 角色都看模型自身视角；MCM 判官关心 "5 分钟读完抓不抓得到亮点" | 加一个 **Judge-simulation pass**：截取 Title+Abstract+前 2 页+图 caption，让模型扮演 "10 分钟流水线评审"，强制选 keep/discard。低于阈值触发 Writer revision |
| 8 | **没有 ICM 专用 prompt 分支** | ICM (C/D/E/F) 强调 policy/societal impact，跟 MCM 的 hard-modeling 不一样；当前 Writer prompt 是统一模板 | `competition_type` 加 `icm_d` / `icm_e` / `icm_f` 子分支，加 ICM 专用 rubric (stakeholder analysis、policy recommendations、ethical considerations) |
| 9 | **References 数量 ≥15 是硬约束，但来源同质** | Searcher 偏向 arXiv；F 奖偏好工业报告/政府数据/经典教材的多元引用 | Searcher 加来源配额：≥30% 非 arXiv（政府/NGO/经典文献），数据集引用单独成行 |
| 10 | **没有自动 plagiarism / anonymity 体检** | 校名/地名/姓名一次违反 = 直接 DQ；当前只在 prompt 里告诫 | 加 post-process regex+NER 扫描器，扫学校词典、中国地名词典、姓名词典，违规直接 fail run |

### 🟡 P2 — 完成度抛光（不是 F 奖门槛但能提分）

| # | 差距 | 建议 |
|---|---|---|
| 11 | 摘要质量没有独立打分 | 单独跑 abstract-quality model 打分（结构、数字、kicker），低分 revise |
| 12 | 图表跟正文不互文 | Critic 加 `figure-discussion-coverage` 检查（每个 `[[FIG:<id>]]` 周围 ±200 字符必须出现 caption 里的数字 token） |
| 13 | 没有 30/60/90 分钟 milestone | MCM 真实场景 = 3 天 96 小时；可以加预算 budget tracker (cost_rmb + 实际 wall clock)，在 25%/50%/75% 时 emit milestone |
| 14 | LaTeX 模板没有官方 mcmthesis | yicheng 仓库里就有 `美赛LaTex模板/mcmthesis`，可以直接采用，比 pandoc default 更接近真实提交格式 |
| 15 | 没有"提交前打包"步骤 | F 奖提交 = 控制号 + PDF + 代码 + summary；当前只产 paper.pdf，缺一键提交包 |

## 已下载得奖论文 + 题目语料（本次新增）

存放位置：`data/mcm/`（已加入 .gitignore 建议）

| 来源 | 内容 | 用途 |
|---|---|---|
| `repos/dick20/` (2.3k★) | MCM/ICM 2004-2025 Outstanding (PDF + MATLAB/TeX) | **few-shot 主库**（差距 #2） |
| `repos/guanglun/` (365★) | 2023-2026 winning papers + 源码 | 最新年份覆盖 |
| `repos/yicheng/` (68★) | 2012-2016 Outstanding 论文集 + mcmthesis 模板 + 算法大全 | 模板（差距 #14） |
| `comap_problems/` | 1999-2025 官方题目 PDF + judge commentary PDF | Analyzer 输入素材 / Critic 评分基准 |
| `early_problems/` | 1985-1999 早期题目 PDF | 题目库完整性 |

## 推荐落地顺序

1. **本周（差距 #2 + #14）**：把 dick20 论文按"年份/题目/主题"切片做 BM25 索引，Writer prompt 加 top-3 同题型 abstract+目录 注入。同时切换 mcmthesis 模板。
2. **下周（差距 #1 + #4）**：DataLoader agent；Writer 后挂 dry-compile gate。
3. **再下周（差距 #5 + #7 + #10）**：Critic 加 evidence-mining + judge-simulation；anonymity 扫描器作 fail-fast gate。
4. **Phase 4 一并（差距 #3）**：云沙箱（E2B/Daytona）。

---

**Why this matters**：F 奖年度全球只有约 1-2% 的队伍能拿到（按 2024 数据约 100-200 篇 / 全球 14000+ 队），它的门槛不是"会写论文"，而是"在 3 天内拿出可读、可信、可复现、亮点鲜明的论文"。我们目前的 prompt 已经在"会写"层面追上了，但**亮点鲜明 = 真实数据 + 充分敏感性 + 独到对比** 是当前最大欠缺。
