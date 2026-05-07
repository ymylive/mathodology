# Phase 4 Critic Loop Design

## Goal

Phase 4 turns the current five-agent pipeline into a reviewed pipeline. Each major agent output is checked by a Critic before downstream agents consume it, and failed checks trigger one bounded revision pass using the Critic's structured feedback.

## Current State

The repository currently runs:

`Analyzer -> Searcher -> Modeler -> Coder -> Writer`

`critic` is already present in the shared event agent enum, but there is no `CriticAgent`, critic prompt, critique data model, pipeline integration, UI stage, or test coverage. Phase 4 therefore starts from protocol reservation, not from a partial implementation.

## Functional Scope

Phase 4 ships the smallest useful critic system:

1. A structured `CritiqueReport` contract with pass/fail, severity, findings, required changes, and optional revised-output hints.
2. A reusable `CriticAgent` that reviews one artifact at a time against stage-specific criteria.
3. A `revise_with_critique` capability for LLM-backed agents that asks the producing agent to revise its own JSON output once.
4. Pipeline gates after Analyzer, Modeler, Coder, and Writer.
5. Stage events and UI support for critic runs so users can see review progress and final findings.
6. Tests that prove both paths: accepted output continues unchanged, rejected output is revised before downstream consumption.

Searcher is excluded from the first Phase 4 implementation because its external-source failure behavior already degrades gracefully and its output is advisory for Writer. It can be reviewed later if needed.

## Review Gates

### Analyzer Gate

Checks that the analysis covers all problem sub-questions, states assumptions, lists data needs, and proposes usable approaches. A failed critique asks Analyzer to revise `AnalyzerOutput`.

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

The report is strict JSON with `extra="forbid"` to keep UI and pipeline behavior stable.

## Pipeline Behavior

Each reviewed stage follows this pattern:

1. Produce the stage output.
2. Run Critic on the output and context.
3. Emit `agent.output` for `CritiqueReport` under agent `critic`.
4. If `passed` is true, continue.
5. If `passed` is false and blocking/major findings exist, run one revision pass.
6. Critic reviews the revised output once.
7. If still failed with blocking findings, fail the run with a clear error.
8. If still failed but only minor findings remain, continue and emit a warning.

The default maximum revision count is one. This prevents infinite agent loops and keeps cost predictable.

## Frontend Behavior

The Workbench should show Critic as a real stage in the stage pills. Because Critic runs multiple times, the UI should aggregate all critic `stage.start`, `stage.done`, `agent.output`, and `cost` events under a single Critic pill and list individual reports in the event/output area.

`AgentOutputView` should render `CritiqueReport` with:

- pass/fail status
- score
- target agent/schema
- summary
- findings grouped by severity
- required changes

## Out Of Scope

- Multi-round refinement beyond one revision.
- Separate specialist critics per agent file.
- Human-in-the-loop approvals.
- Searcher review.
- Automatic issue creation from critique failures.
- Cloud sandbox or production auth changes from Phase 5.

## Acceptance Criteria

1. A clean run with passing critiques completes successfully and emits Critic events.
2. A run with one failed Modeler critique revises the `ModelSpec` and continues with the revised spec.
3. A run with unresolved blocking Writer findings fails with a clear run error.
4. Unit tests cover `CritiqueReport`, `CriticAgent`, revise prompts, and pipeline gate behavior.
5. Frontend stage pills include Critic and render critique reports.
6. Existing tests for Analyzer, Modeler, Coder, Writer, Searcher, exports, and contracts remain passing.
