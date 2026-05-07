# Phase 4 Critic Loop Design

## Goal

Phase 4 turns the current five-agent pipeline into a reviewed pipeline. Phase 4 MVP checked major stage outputs with a single Critic pass. Phase 4.1 upgrades that into a multi-role post-output Critic loop with deterministic thresholds, bounded self-refine rounds, and Coder iteration coordination.

## Current State

The repository currently runs:

`Analyzer -> Searcher -> Modeler -> Coder -> Writer`

The repository now has a `CriticAgent`, structured critique contracts, pipeline gates, UI rendering, and tests. Phase 4.1 builds on that shipped branch state rather than starting from protocol reservation.

## Functional Scope

Phase 4.1 ships the stricter critic system:

1. A structured `CritiqueReport` contract with pass/fail, severity, findings, required changes, role reviews, checklist items, revision metadata, and budget exhaustion.
2. A reusable `CriticAgent` that reviews one artifact at a time using target-specific roles.
3. A `revise_with_critique` capability for Analyzer, Searcher, Modeler, and Writer structured outputs.
4. Pipeline gates after Analyzer, Searcher, Modeler, Coder, and Writer.
5. A bounded `critique -> revise -> critique` loop with default two revision rounds.
6. Deterministic thresholds: score, checklist pass rate, blocking findings, and major finding counts.
7. Coder corrective passes capped separately from the award-mode `MAX_ITER=7` loop.
8. Stage events and UI support for role-based critic reports.

## Review Gates

### Analyzer Gate

Checks that the analysis covers all problem sub-questions, states assumptions, lists data needs, and proposes usable approaches. A failed critique asks Analyzer to revise `AnalyzerOutput`.

### Searcher Gate

Checks source quality, citation coverage, relevance, and empty-result handling. A failed critique asks Searcher to revise `SearchFindings` without re-running external search tools or inventing new sources.

### Modeler Gate

Checks that the model spec is internally consistent, maps to the analysis, defines variables/equations clearly, includes validation, and avoids method mismatch. A failed critique asks Modeler to revise `ModelSpec`.

### Coder Gate

Checks that executed cells support the model, include validation/sensitivity evidence, register figures correctly, and report useful numerical results. A failed critique asks Coder for one corrective iteration using the existing notebook context.

### Writer Gate

Checks final paper quality against award-mode rules: abstract, all sub-questions, citations, figures, sensitivity analysis, strengths/weaknesses, anonymity, and numeric result discipline. A failed critique asks Writer to revise `PaperDraft`.

## Data Model

Add to `mm_contracts.agent_io`:

- `CritiqueSeverity = Literal["info", "minor", "major", "blocking"]`
- `CritiqueFinding`
  - `severity`
  - `area`
  - `message`
  - `evidence`
  - `required_change`
- `CritiqueReport`
  - `target_agent`
  - `target_schema`
  - `passed`
  - `score`
  - `summary`
  - `findings`
  - `required_changes`
  - `roles`
  - `checklist`
  - `revision_round`
  - `max_revision_rounds`
  - `budget_exhausted`

The report is strict JSON with `extra="forbid"` to keep UI and pipeline behavior stable.

## Pipeline Behavior

Each reviewed stage follows this pattern:

1. Produce the stage output.
2. Run Critic on the output and context.
3. Emit `agent.output` for `CritiqueReport` under agent `critic`.
4. If `passed` is true, continue.
5. If policy thresholds fail, run a bounded revision pass.
6. Critic reviews the revised output and the loop repeats until pass or budget exhaustion.
7. If blocking findings remain after the revision budget, fail the run with a clear error.
8. If the budget is exhausted without blocking findings, continue with the latest revised output and the critique report marks the budget state.

The default maximum revision count is two. `CriticPolicy` also includes score threshold `0.80`, checklist pass-rate threshold `0.85`, Coder corrective iteration cap `2`, and a local revision-loop cost budget.

## Frontend Behavior

The Workbench should show Critic as a real stage in the stage pills. Because Critic runs multiple times, the UI should aggregate all critic `stage.start`, `stage.done`, `agent.output`, and `cost` events under a single Critic pill and list individual reports in the event/output area.

`AgentOutputView` should render `CritiqueReport` with:

- pass/fail status
- score
- checklist pass rate
- revision round / max rounds
- budget exhaustion marker
- target agent/schema
- summary
- role-specific verdicts
- findings grouped by severity
- required changes

## Out Of Scope

- Separate specialist critic agent files.
- Human-in-the-loop approvals.
- Automatic issue creation from critique failures.
- Cloud sandbox or production auth changes from Phase 5.
-- Full real-time DB cost-ledger enforcement inside worker. Phase 4.1 adds a local loop budget interface; gateway cost accounting remains authoritative for run totals.

## Acceptance Criteria

1. A clean run with passing critiques completes successfully and emits Critic events.
2. A run with failed Modeler critiques can revise the `ModelSpec` up to two times and continues with the revised spec when thresholds pass.
3. A run with unresolved blocking Writer findings fails with a clear run error.
4. Unit tests cover `CritiqueReport`, role-aware `CriticAgent`, revise prompts, thresholds, Coder iteration caps, and pipeline gate behavior.
5. Frontend stage pills include Critic and render role/checklist critique reports.
6. Existing tests for Analyzer, Modeler, Coder, Writer, Searcher, exports, and contracts remain passing.
