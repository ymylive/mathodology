# Phase 4.1 Multi-role Critic Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Phase 4 from a single bounded Critic pass to a multi-role post-output self-refine loop with explicit thresholds, revision budgets, Searcher coverage, and Coder iteration coordination.

**Architecture:** Keep one `CriticAgent` orchestration surface, but make its contract role-aware and policy-driven. Pipeline helpers own deterministic pass/fail thresholds and retry budgets; prompts provide specialist reviews but do not decide budget or control flow alone.

**Tech Stack:** Python 3.12, Pydantic v2 contracts, pytest/pytest-asyncio, Vue 3 TypeScript, pnpm build, existing Rust gateway cost events.

---

### Task 1: Extend Critique Contracts

**Files:**
- Modify: `packages/py-contracts/src/mm_contracts/agent_io.py`
- Modify: `packages/py-contracts/src/mm_contracts/__init__.py`
- Modify: `packages/ts-contracts/src/index.ts`
- Test: `apps/agent-worker/tests/test_critic_contracts.py`

- [ ] Add failing tests for role reviews, checklist pass rate, revision metadata, and `searcher` as a review target.
- [ ] Run `uv run pytest apps/agent-worker/tests/test_critic_contracts.py -q` and verify the new tests fail.
- [ ] Add `CriticRole`, `CritiqueChecklistItem`, and `RoleCritique` models.
- [ ] Extend `CritiqueReport` with `roles`, `checklist`, `revision_round`, `max_revision_rounds`, and `budget_exhausted`.
- [ ] Add computed helpers for checklist pass rate, blocking findings across roles, and major finding count.
- [ ] Mirror the hand-written TypeScript contract changes in `packages/ts-contracts/src/index.ts`.
- [ ] Re-run the contract tests and commit.

### Task 2: Add Threshold Policy Helpers

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_pipeline_critic_gate.py`

- [ ] Add failing tests for score threshold `< 0.80`, checklist pass rate `< 0.85`, two major findings, blocking findings, and budget exhaustion.
- [ ] Run `uv run pytest apps/agent-worker/tests/test_pipeline_critic_gate.py -q` and verify the new tests fail.
- [ ] Introduce a small `CriticPolicy` configuration with defaults: `min_score=0.80`, `min_checklist_pass_rate=0.85`, `max_revision_rounds=2`, `coder_revision_iterations=2`.
- [ ] Update `_critique_requires_revision` and `_critique_should_fail_run` to use policy plus report fields.
- [ ] Re-run the gate tests and commit.

### Task 3: Make Critic Role-aware

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/agents/critic.py`
- Modify: `apps/agent-worker/src/agent_worker/prompts/critic/v1.toml`
- Test: `apps/agent-worker/tests/test_critic_agent.py`

- [ ] Add failing tests that `CriticAgent.review` sends target-specific role lists and checklist items to the prompt.
- [ ] Run `uv run pytest apps/agent-worker/tests/test_critic_agent.py -q` and verify the new tests fail.
- [ ] Add role selection defaults:
  - Analyzer: `modeling_coach`, `academic_reviewer`
  - Searcher: `academic_reviewer`
  - Modeler: `modeling_coach`, `academic_reviewer`
  - Coder: `modeling_coach`, `code_reviewer`
  - Writer: `academic_reviewer`, `modeling_coach`
- [ ] Update the Critic prompt to require role-separated findings, role scores, and checklist evidence.
- [ ] Re-run the CriticAgent tests and commit.

### Task 4: Replace One-pass Revision With Bounded Self-refine

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_pipeline_critic_review_flow.py`

- [ ] Add failing async tests showing two revision rounds are allowed and the loop stops after the budget.
- [ ] Run `uv run pytest apps/agent-worker/tests/test_pipeline_critic_review_flow.py -q` and verify the new tests fail.
- [ ] Rewrite `_review_and_maybe_revise` as a `critique -> revise -> critique` loop using `CriticPolicy.max_revision_rounds`.
- [ ] Preserve the original behavior for a passing first critique: no revision calls, one Critic call.
- [ ] Fail the run only when blocking findings remain after budget or `budget_exhausted` is true with blocking findings.
- [ ] Re-run the flow tests and commit.

### Task 5: Cap Coder Corrective Iterations

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/agents/coder.py`
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_coder_agent.py`
- Test: `apps/agent-worker/tests/test_coder_critic_revision.py`

- [ ] Add failing tests that `CoderAgent.run(..., max_iterations=2)` executes at most two LLM/code cells.
- [ ] Add failing tests that `_review_and_maybe_rerun_coder` uses the policy corrective iteration cap.
- [ ] Run `uv run pytest apps/agent-worker/tests/test_coder_agent.py apps/agent-worker/tests/test_coder_critic_revision.py -q` and verify the new tests fail.
- [ ] Add optional `max_iterations` parameter to `CoderAgent.run`, defaulting to `MAX_ITERATIONS`.
- [ ] Use the policy's `coder_revision_iterations=2` for Critic corrective Coder passes.
- [ ] Re-run the coder tests and commit.

### Task 6: Review Searcher Output

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_pipeline_critic_review_flow.py`

- [ ] Add a failing test or focused pipeline helper coverage that Searcher output can be reviewed with `target_agent="searcher"`.
- [ ] Run the focused pipeline tests and verify failure.
- [ ] Add Searcher criteria around source quality, citation usability, relevance, and graceful empty-result behavior.
- [ ] Insert Searcher review after `searcher.run_for(...)` and before Modeler/Writer consume findings.
- [ ] Re-run focused pipeline tests and commit.

### Task 7: Render Role-based Critique Reports

**Files:**
- Modify: `apps/web/src/components/CritiqueReport.vue`
- Modify: `packages/ts-contracts/src/index.ts`

- [ ] Update the component to show role reviews, checklist pass rate, revision rounds, and budget exhaustion.
- [ ] Keep compatibility with older reports that lack role/checklist fields by using empty defaults.
- [ ] Run `pnpm --filter web build`.
- [ ] Commit the UI changes.

### Task 8: Regenerate / Verify Contracts and Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-07-phase4-critic-loop-design.md`
- Modify if generated: `packages/ts-contracts/src/generated.ts`
- Test: `tests/test_contracts_drift_workflow.py`

- [ ] Update docs to distinguish Phase 4 MVP from Phase 4.1 multi-role loop.
- [ ] Run the contracts drift workflow test.
- [ ] Run focused Python tests, `cargo test --workspace`, and `pnpm --filter web build`.
- [ ] Commit docs and generated contract updates.

### Verification Checklist

- [ ] `uv run pytest apps/agent-worker/tests/test_critic_contracts.py apps/agent-worker/tests/test_critic_agent.py apps/agent-worker/tests/test_pipeline_critic_gate.py apps/agent-worker/tests/test_pipeline_critic_review_flow.py apps/agent-worker/tests/test_coder_critic_revision.py -q`
- [ ] `uv run pytest apps/agent-worker -q`
- [ ] `cargo test --workspace`
- [ ] `pnpm --filter web build`
- [ ] `git status -sb` shows only intended committed changes.
