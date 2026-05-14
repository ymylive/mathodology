# MCM/ICM 历年得奖论文 + 题目语料 (3.2G)

> 2026-05-14 批量下载，三仓 git clone 合并。用途：(1) 为 Writer/Modeler prompt 注入 few-shot 同题型 abstract+sensitivity 章节示例；(2) 给 Critic 做"判官视角"的 anchor；(3) Coder 直接读真实数据附件做实验。

## 奖项覆盖（重要）

**本语料同时包含 O 奖 + F 奖 + 其它奖**，且 **O 奖（Outstanding Winner，特等奖）是绝对主体**。

| 奖项 | 中文 | 全球获奖率 | 在本语料中的位置 |
|---|---|---|---|
| **Outstanding Winner** | **O 奖 / 特等奖** | ~0.2-0.5% | **dick20 主目录 + yicheng 全部，~500+ 篇**（主体） |
| **Finalist** | **F 奖** | ~1-2% | dick20 `其他奖项/*-Finalist.pdf`（明确标 3 篇，更多与 O 奖混存在 `A/B/C/D/E/F/`） |
| Meritorious | M 奖 / 一等奖 | ~7-9% | dick20 `其他奖项/` 部分；非主要 |
| Honorable Mention | H 奖 / 二等奖 | ~21% | 不收 |
| Successful Participant | S 奖 | rest | 不收 |

> ⚠️ **dick20 / yicheng 命名"特等奖"= Outstanding Winner**。但 COMAP 实际每年每题只评 1-2 篇 Outstanding（全年合计约 30-40 篇），仓库里 502 篇 PDF 远超此数 — 所以 `YYYY美赛特等奖/A/B/C/.../` 子目录里**实际是 Outstanding + Finalist 合集**（中文圈把 F 奖也常归为"特等奖级别"）。判官评语 (`Results/*_Judges_Commentary_*.pdf`) 里有 Outstanding 队伍的 control number，可以反向精确切分。

## 总量

| 项 | 数量 |
|---|---|
| 论文/题目 PDF 总数 | **651** |
| O 奖 + F 奖论文（2004-2025，22 年） | ~500 |
| 题目 PDF（problem statements） | **67** |
| 数据附件（CSV / XLSX / ZIP） | **34** |
| 文件名带 "Finalist" 字样 | 3（其余 F 奖论文与 O 奖混在 `A/B/...` 目录） |
| 总占用 | **3.2 GB** |

## 仓库结构

```
data/mcm/
├── GAP_ANALYSIS.md            ← 差距分析（项目→F 奖距离 + ROI 排序的 15 项 backlog）
├── README.md                  ← 本文件
├── logs/                      ← 旧抓取日志（COMAP 直抓失败，已 pivot 到 git clone）
└── repos/
    ├── dick20/   (2.1 GB, 502 PDFs)   ← 主语料：2004-2025 美赛特等奖
    ├── guanglun/ (886 MB,  49 PDFs)   ← 2023-2026 + 源码 + 图素材
    └── yicheng/  (265 MB, 100 PDFs)   ← 2012-2016 + mcmthesis LaTeX 模板
```

### `repos/dick20/` （核心语料）

22 个年度目录，每年命名 `YYYY美赛特等奖/`：

```
2024美赛特等奖/
├── problems/                     ← 题目 PDF + 真实数据附件
│   ├── 2024_MCM_Problem_A_FINAL.pdf
│   ├── 2024_MCM_Problem_C_FINAL.pdf
│   ├── 2024_ICM_Problem_D_FINAL.pdf
│   ├── Problem_D_Addendum.pdf
│   ├── Problem_D_Great_Lakes.xlsx   ← 真实附件！
│   ├── Wimbledon_featured_matches.csv
│   └── data_dictionary.csv
├── A/   ← 该年 A 题 Outstanding 论文（PDF 命名为 control number, e.g. 2425397.pdf）
├── B/
├── C/
└── ...
```

**目录组织随年份演化**：
- **2004-2009**：扁平 + `Results/` 子目录（含 Judge Commentary）
- **2010-2017**：按题目 letter (`A题5篇/` 或 `A/`) 分；早期年份题目 PDF 在 `Results/`
- **2018**：`2018_MCM-ICM_Problems/` 单独目录放题目
- **2019**：`2019_MCM-ICM_Problems/` + 数据附件 (`ACS_*_DP02.zip`, `MCM_NFLIS_Data.xlsx`)
- **2020+**：`problems/` 统一目录；`A/B/C/D/E/F/` 平铺论文

**每年 Outstanding 论文数**：
```
2004:  5    2010: 17    2016: 37    2022: 50
2005:  5    2011: 18    2017: 27    2023: 37
2006: 12    2012: 21    2018: 38    2024: 12 (incomplete)
2007: 10    2013: 17    2019: 41    2025:  8 (incomplete)
2008:  7    2014: 25    2020: 37
2009:  6    2015: 29    2021: 43
```

**精确切分 O 奖 vs F 奖（如果需要）**：
- 文件名带 `-Finalist` 的明确是 F 奖（仅 3 篇，2011/2013/2015 各 1 篇）
- 文件名是纯 control number（如 `2425397.pdf`）的需要交叉对照 `Results/*_Judges_Commentary*.pdf`：评语里点名的队伍 = Outstanding；其余按"特等奖"目录收录的 = Finalist
- 对**当前用途（prompt few-shot）**：O 奖 + F 奖论文质量足够接近，**无须区分**，直接全用即可

### `repos/guanglun/` （最新年份 + 源码）

```
guanglun/
├── 2023_MCM_Problem_B.pdf
├── 2023美赛B题-中文.pdf
├── 2316192.pdf                   ← 一篇 2023 完整论文
├── Code/                          ← Python 源码
├── Data/                          ← 数据集
├── Figure/                        ← 图素材库（生化环材类/机器学习/prize 三类）
├── Word/                          ← Word 草稿
├── AI-Report-2024/                ← 2024 年作者用 AI 协作的 report
└── Note/
    ├── 近年优秀论文资源/
    └── O奖得主分享材料/            ← 西电 O 奖 2020 + 福州大学 O 奖 2022
```

### `repos/yicheng/` （早期 + 模板）

```
yicheng/
├── 2012美国大学生数学建模特等奖论文集/
├── 2013美国大学生数学建模特等奖论文集/
├── 2014美国大学生数学建模特等奖论文集/
├── 2015美国大学生数学建模特等奖论文集/
├── 2016美国大学生数学建模特等奖论文集/
├── 美赛LaTex模板/mcmthesis/        ← 真实 mcmthesis 模板（差距分析 #14 建议采用）
└── 算法大全pdf/                    ← 司守奎《数学建模算法与应用》PDF
```

## 重新生成 / 更新

```bash
cd data/mcm/repos
git -C dick20    pull   # 增量更新
git -C guanglun  pull
git -C yicheng   pull
```

## 缺失的部分

- **1985-2003 题目 + 论文**：COMAP 把题目搬到 `comap.org/membership/member-resources/...` 会员墙；论文从未公开
- **2024-2025 完整 Outstanding 集**：dick20 还在收集中（2024 只有 12 篇，2025 只有 8 篇）；guanglun 补一部分但不全
- **题目 PDF 完整度**：67 个题目 PDF 覆盖 2010-2025（每年 6 题 × 16 年 = 96，差 ~30 个）；2004-2009 题目大多只能从 Outstanding 论文的 "Problem Statement" 章节反推

## .gitignore 建议

3.2G 不适合入库。建议项目根 `.gitignore` 追加：
```
data/mcm/repos/
data/mcm/logs/
```
仅提交 `GAP_ANALYSIS.md` 和 `README.md`。

## 下一步使用（对照 GAP_ANALYSIS.md）

| 差距编号 | 操作 |
|---|---|
| #2（缺得奖论文 few-shot） | 用 dick20 / yicheng 切片：`pdfplumber` 提 abstract，按 (year, problem_letter, prize_level) 索引到 HMML 或新建 `award_corpus/` 表 |
| #14（mcmthesis 模板） | 把 `yicheng/美赛LaTex模板/mcmthesis/` 拷到 `apps/agent-worker/.../prompts/templates/` 替换 pandoc default |
| #1（真实数据接入） | DataLoader agent 读 `dick20/YYYY美赛特等奖/problems/*.{csv,xlsx,zip}` 做训练用例 |
| #10（anonymity 扫描） | 用 dick20 论文 ground truth 反向验证（这些都是去名版本，不应误报） |
