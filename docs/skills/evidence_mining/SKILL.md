---
name: evidence_mining
description: Deterministic post-Writer scans for sensitivity-analysis coverage (≥3 parameters at ±N%) and anonymity violations (school / region / advisor leaks). Adds blocking findings to Critic criteria.
when_to_use:
  - "running between Writer.run_for() and Critic.review()"
  - "validating that an Outstanding-level paper meets the empirical bar"
allowed-tools: []
context: inline
---

# Evidence Mining — Deterministic Empirical Checks

Two regex-driven scanners that run between `Writer.run_for()` and
`Critic.review()`. They produce *deterministic* `BLOCKING:` criteria that
the Critic is forced to evaluate, so a paper cannot pass review just because
the model self-reports strong empirical work.

Source: `apps/agent-worker/src/agent_worker/agents/evidence.py`.

## Why deterministic scans exist

LLM-only Critics drift: they over-trust the Writer's claims about sensitivity
work, and they consistently fail to catch identity leaks that disqualify
papers under the MCM/CUMCM honour code. Both failure modes are cheap to
detect with regex if you accept some recall loss in exchange for high
precision.

## Scanner 1 — `mine_sensitivity_evidence`

**Goal.** Confirm the paper contains a "Sensitivity Analysis / 敏感性分析"
section that perturbs at least 3 distinct parameters with quantitative
deltas. This is the empirical bar that distinguishes Outstanding-tier from
median CUMCM papers.

**Patterns scanned.**

- `_SENS_HEADING_RE` — case-insensitive match for "sensitivity analysis",
  "sensitivity study", "敏感性分析", "灵敏度分析" in any section title or body.
- `_PERTURB_RE` — perturbation phrasing: `±N%`, `+/- N%`, "plus or minus",
  "增减", "变化", "波动", "扰动" followed by a number, optional `%`.
- `_PCT_DELTA_RE` — explicit `N%` deltas that report objective swings
  ("the objective fell by 4.3%").
- `_SENS_FIGURE_TOKENS` — references to canonical sensitivity figures:
  `tornado`, `heatmap_sensitivity`, `monte_carlo`, "敏感性", "灵敏度".
- `_PARAM_PATTERNS` — three parameter-binding shapes:
  1. `perturbed α by ±10%` / `varying β over ±20%`
  2. `α by ±10%` / `X_init by 5%`
  3. `For α at ±10%` / `对 α 增减 10%`

The parameter-count proxy collects the matched group(1) tokens into a
`set[str]`, after filtering a small blacklist of English stop-words
(`the`, `a`, `for`, …) and capping token length at 30 chars. Conservative
on purpose — over-counting is harmless; missing a real parameter would
let a weak paper through.

**Pass condition.** `passes_award_bar()` returns True iff:

- a sensitivity heading is present, AND
- ≥3 distinct parameter tokens were identified, AND
- ≥3 quantitative perturbation mentions were found in body text.

**Critic injection.** `sensitivity_criteria(findings)` returns a list of
`BLOCKING: …` strings, one per gap:

- No sensitivity section detected.
- `<3` parameter tokens found.
- `<3` `±N%` mentions in the body.
- No `tornado_sensitivity` / `heatmap_sensitivity` / Monte-Carlo figure
  referenced (the Modeler's plan requires one of these).

These criteria append to `CriticInputs.criteria` so the Critic must
explicitly accept or reject them — they cannot be silently ignored.

## Scanner 2 — `scan_anonymity_violations`

**Goal.** Catch the instant-disqualification leaks: real school name, real
region, real author/advisor name in title, abstract, sections, or references.

**Pattern groups.**

- `_UNIVERSITY_PATTERNS` — 27 universities that show up in the leak history,
  in both English and Chinese spellings (Tsinghua, Peking, Fudan, Jiao Tong,
  Zhejiang, Beihang, USTC, Nanjing, Xi'an JTU, HUST, HIT, Sun Yat-sen,
  Wuhan, Tongji, Southeast, NUDT, and Chinese-character variants).
- `_REGION_PATTERNS` — tier-1 cities and CN provinces (Beijing, Shanghai,
  Shenzhen, Chengdu, Wuhan, 北京, 上海, …, 吉林省, 浙江省, 广东省).
- `_AUTHOR_PATTERNS` — leak-shaped phrases: "I am", "My name is", "We are
  students at", "作者: …", "指导教师: …".

All patterns are compiled with `re.IGNORECASE | re.MULTILINE` and joined
into a single list scanned against:

- `paper.title`
- `paper.abstract`
- each `section.title` and `section.body_markdown`
- `paper.references` joined with `\n`

Each hit captures 30 chars of context on either side. The scan short-circuits
after 5 violations (enough for the Critic to act on; full sweep is the
human reviewer's job).

**Critic injection.** `anonymity_criteria(findings)` returns either:

- `[]` if no violations, or
- a single `BLOCKING (DISQUALIFICATION RISK): ...` criterion with up to 5
  bullet-pointed snippets pointing at the locations.

## Critic-criteria injection point

The orchestrator calls both scans between Writer and Critic:

```python
from agent_worker.agents.evidence import (
    anonymity_criteria,
    mine_sensitivity_evidence,
    scan_anonymity_violations,
    sensitivity_criteria,
)

paper = await writer.run_for(stage="writer", ...)

extra_crits: list[str] = []
extra_crits.extend(sensitivity_criteria(mine_sensitivity_evidence(paper)))
extra_crits.extend(anonymity_criteria(scan_anonymity_violations(paper)))

critic_inputs.criteria = [*critic_inputs.criteria, *extra_crits]
verdict = await critic.review(critic_inputs)
```

This is the only place the scanners run. Do NOT call them inside the
Writer's loop — the Writer's revision pass would game them.

## Failure modes & precision/recall trade-offs

| Mode                              | Behaviour                                | Owner       |
|-----------------------------------|------------------------------------------|-------------|
| Sensitivity count slightly low    | Critic appends `BLOCKING:` → revision    | Writer      |
| Sensitivity count zero            | Critic blocks → typically full rewrite   | Writer      |
| Anonymity leak in body            | Critic blocks with snippet locations     | Writer      |
| False-positive university match   | Critic still raises — human acks         | Reviewer    |
| Parameter token in `_BLACKLIST`   | Silently dropped (e.g. "We", "the")      | Loader      |
| Greek-letter param without binding | Missed; rely on perturbation total       | Conservative |

Recall is intentionally biased high on the anonymity side (we'd rather
flag 1 extra false positive than miss a DQ) and biased high on the
sensitivity side too (we'd rather force one more revision pass than ship
a thin paper). Both biases are documented in the source.

## Testing

`apps/agent-worker/tests/test_evidence_mining.py` — covers each pattern
group with positive and negative examples, plus an end-to-end run that
checks the appended `BLOCKING:` strings are well-formed for the Critic.
