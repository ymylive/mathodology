# Phase 4 Critic Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded Critic review loop that checks Analyzer, Modeler, Coder, and Writer outputs, asks the producing agent for one revision when needed, and exposes critique reports in the Workbench UI.

**Architecture:** Add strict shared critique contracts, a reusable LLM-backed `CriticAgent`, and small pipeline gate helpers around existing stage calls. Keep the first implementation bounded to one revision per reviewed stage and one aggregate Critic UI stage.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, Redis event stream, existing GatewayClient/BaseAgent prompt framework, Vue 3 + Pinia + TypeScript.

---

## File Structure

- `packages/py-contracts/src/mm_contracts/agent_io.py`: add `CritiqueFinding` and `CritiqueReport`.
- `packages/ts-contracts/src/index.ts`: add TypeScript critique types for the UI.
- `packages/contracts/events.schema.json`: keep `critic` supported and document critique output payload expectations.
- `apps/agent-worker/src/agent_worker/prompts/critic/v1.toml`: critic prompt and JSON output schema name.
- `apps/agent-worker/src/agent_worker/agents/critic.py`: new `CriticAgent`.
- `apps/agent-worker/src/agent_worker/agents/base.py`: add revision support for structured JSON outputs.
- `apps/agent-worker/src/agent_worker/agents/coder.py`: add one corrective revision pass hook for coder output.
- `apps/agent-worker/src/agent_worker/pipeline.py`: add review gates after Analyzer, Modeler, Coder, Writer.
- `apps/web/src/components/CritiqueReport.vue`: render critique report output.
- `apps/web/src/components/AgentOutputView.vue`: dispatch `CritiqueReport`.
- `apps/web/src/components/StagePills.vue`: add aggregate Critic pill.
- Tests under `apps/agent-worker/tests/` and frontend type/build checks.

---

### Task 1: Add Critique Contracts

**Files:**
- Modify: `packages/py-contracts/src/mm_contracts/agent_io.py`
- Modify: `packages/ts-contracts/src/index.ts`
- Test: `apps/agent-worker/tests/test_critic_contracts.py`

- [ ] **Step 1: Write the failing Python contract tests**

Create `apps/agent-worker/tests/test_critic_contracts.py`:

```python
from __future__ import annotations

import pytest
from mm_contracts import CritiqueFinding, CritiqueReport
from pydantic import ValidationError


def test_critique_report_accepts_blocking_findings() -> None:
    report = CritiqueReport(
        target_agent="modeler",
        target_schema="ModelSpec",
        passed=False,
        score=0.42,
        summary="The selected method does not match the problem constraints.",
        findings=[
            CritiqueFinding(
                severity="blocking",
                area="method fit",
                message="The model ignores the time-varying demand constraint.",
                evidence="No variable or equation covers demand over time.",
                required_change="Add time-indexed demand variables and constraints.",
            )
        ],
        required_changes=["Revise the model around time-indexed demand."],
    )

    assert report.target_agent == "modeler"
    assert report.has_blocking_findings is True
    assert report.findings[0].severity == "blocking"


def test_critique_report_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CritiqueReport.model_validate(
            {
                "target_agent": "writer",
                "target_schema": "PaperDraft",
                "passed": True,
                "score": 0.91,
                "summary": "Looks good.",
                "findings": [],
                "required_changes": [],
                "unexpected": "nope",
            }
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_critic_contracts.py -q
```

Expected: FAIL because `CritiqueFinding` and `CritiqueReport` are not importable.

- [ ] **Step 3: Add Python contracts**

In `packages/py-contracts/src/mm_contracts/agent_io.py`, add these models after `AgentEvent`:

```python
CritiqueSeverity = Literal["info", "minor", "major", "blocking"]
ReviewTargetAgent = Literal["analyzer", "modeler", "coder", "writer"]


class CritiqueFinding(BaseModel):
    """One concrete issue found by the Critic."""

    model_config = ConfigDict(extra="forbid")

    severity: CritiqueSeverity
    area: str
    message: str
    evidence: str
    required_change: str


class CritiqueReport(BaseModel):
    """Structured Critic output for one reviewed agent artifact."""

    model_config = ConfigDict(extra="forbid")

    target_agent: ReviewTargetAgent
    target_schema: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    summary: str
    findings: list[CritiqueFinding] = Field(default_factory=list, max_length=20)
    required_changes: list[str] = Field(default_factory=list, max_length=20)

    @property
    def has_blocking_findings(self) -> bool:
        return any(f.severity == "blocking" for f in self.findings)

    @property
    def has_major_findings(self) -> bool:
        return any(f.severity == "major" for f in self.findings)
```

Also ensure these names are exported by `packages/py-contracts/src/mm_contracts/__init__.py` if that file lists exports explicitly.

- [ ] **Step 4: Add TypeScript contracts**

In `packages/ts-contracts/src/index.ts`, add:

```ts
export type CritiqueSeverity = "info" | "minor" | "major" | "blocking";
export type ReviewTargetAgent = "analyzer" | "modeler" | "coder" | "writer";

export interface CritiqueFinding {
  severity: CritiqueSeverity;
  area: string;
  message: string;
  evidence: string;
  required_change: string;
}

export interface CritiqueReport {
  target_agent: ReviewTargetAgent;
  target_schema: string;
  passed: boolean;
  score: number;
  summary: string;
  findings: CritiqueFinding[];
  required_changes: string[];
}
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_critic_contracts.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/py-contracts/src/mm_contracts/agent_io.py packages/py-contracts/src/mm_contracts/__init__.py packages/ts-contracts/src/index.ts apps/agent-worker/tests/test_critic_contracts.py
git commit -m "feat(critic): add critique report contracts"
```

---

### Task 2: Add CriticAgent

**Files:**
- Create: `apps/agent-worker/src/agent_worker/prompts/critic/v1.toml`
- Create: `apps/agent-worker/src/agent_worker/agents/critic.py`
- Modify: `apps/agent-worker/src/agent_worker/agents/__init__.py`
- Test: `apps/agent-worker/tests/test_critic_agent.py`

- [ ] **Step 1: Write failing CriticAgent smoke test**

Create `apps/agent-worker/tests/test_critic_agent.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agent_worker.agents import CriticAgent
from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueReport


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(self, kind: str, payload: dict | None = None, agent: str | None = None) -> None:
        self.events.append((kind, payload or {}, agent))


class _FakeGateway:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk


async def test_critic_agent_reviews_analyzer_output() -> None:
    report_json = (
        '{"target_agent":"analyzer",'
        '"target_schema":"AnalyzerOutput",'
        '"passed":true,'
        '"score":0.88,'
        '"summary":"Analysis covers the problem and has usable approaches.",'
        '"findings":[],'
        '"required_changes":[]}'
    )
    emitter = _FakeEmitter()
    gateway = _FakeGateway([report_json])
    critic = CriticAgent(gateway, emitter)  # type: ignore[arg-type]
    artifact = AnalyzerOutput(
        restated_problem="A 20-character restatement of the modeling problem.",
        sub_questions=["Estimate demand", "Optimize allocation"],
        proposed_approaches=[
            ApproachSketch(name="Optimization", rationale="Fits allocation", methods=["LP"])
        ],
    )

    report = await critic.review(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        artifact=artifact.model_dump(mode="json"),
        context={"problem_text": "Optimize allocation under demand uncertainty."},
        criteria=["covers all sub-questions", "lists usable approaches"],
    )

    assert isinstance(report, CritiqueReport)
    assert report.passed is True
    assert report.score == 0.88
    assert [event[0] for event in emitter.events] == [
        "stage.start",
        "agent.output",
        "stage.done",
    ]
    assert all(event[2] == "critic" for event in emitter.events)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_critic_agent.py -q
```

Expected: FAIL because `CriticAgent` does not exist.

- [ ] **Step 3: Add critic prompt**

Create `apps/agent-worker/src/agent_worker/prompts/critic/v1.toml`:

```toml
version = "1.0.0"
agent = "critic"
model_preference = ["deepseek-chat", "moonshot-v1-32k"]
token_budget_in = 24000
token_budget_out = 4000
temperature = 0.1

[system]
text = """
You are the Critic agent for a mathematical-modeling competition pipeline.
Review exactly one upstream artifact. Be strict, concrete, and evidence-based.
Return ONLY a JSON object matching CritiqueReport. Do not include markdown fences.

Severity rules:
- blocking: downstream work would be invalid or the final paper would likely fail judging.
- major: important quality issue that should be revised if possible.
- minor: useful improvement but not a blocker.
- info: observation that does not require action.

Set passed=false when any blocking finding exists or when two or more major findings exist.
Set score from 0.0 to 1.0 based on readiness for downstream use.
"""

[user_template]
text = """
Target agent: {{ target_agent }}
Target schema: {{ target_schema }}

Review criteria:
{{ criteria }}

Context JSON:
{{ context_json }}

Artifact JSON:
{{ artifact_json }}

Respond with a JSON object. Required top-level keys:
  target_agent,
  target_schema,
  passed,
  score,
  summary,
  findings (array of {severity, area, message, evidence, required_change}),
  required_changes (array of strings)
"""

[response_schema]
kind = "json_object"
name = "CritiqueReport"
```

- [ ] **Step 4: Implement CriticAgent**

Create `apps/agent-worker/src/agent_worker/agents/critic.py`:

```python
"""Critic agent: review one upstream artifact and produce a CritiqueReport."""

from __future__ import annotations

import json
from typing import Any, Literal

from mm_contracts import CritiqueReport, ReasoningEffort

from agent_worker.agents.base import BaseAgent
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient

ReviewTarget = Literal["analyzer", "modeler", "coder", "writer"]


class CriticAgent(BaseAgent):
    """Reviews one artifact against explicit criteria."""

    AGENT_NAME = "critic"
    OUTPUT_MODEL = CritiqueReport

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "medium",
        long_context: bool = False,
        model_override: str | None = None,
    ) -> None:
        super().__init__(
            gateway,
            emitter,
            prompt_version,
            run_effort=run_effort,
            long_context=long_context,
            model_override=model_override,
        )

    async def review(
        self,
        *,
        target_agent: ReviewTarget,
        target_schema: str,
        artifact: dict[str, Any],
        context: dict[str, Any],
        criteria: list[str],
    ) -> CritiqueReport:
        output = await self.run(
            target_agent=target_agent,
            target_schema=target_schema,
            artifact_json=json.dumps(artifact, ensure_ascii=False, indent=2),
            context_json=json.dumps(context, ensure_ascii=False, indent=2),
            criteria="\n".join(f"- {item}" for item in criteria),
        )
        assert isinstance(output, CritiqueReport)
        return output


__all__ = ["CriticAgent", "ReviewTarget"]
```

- [ ] **Step 5: Export CriticAgent**

In `apps/agent-worker/src/agent_worker/agents/__init__.py`, add:

```python
from agent_worker.agents.critic import CriticAgent
```

and add `"CriticAgent"` to `__all__`.

- [ ] **Step 6: Run test**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_critic_agent.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/agent-worker/src/agent_worker/prompts/critic/v1.toml apps/agent-worker/src/agent_worker/agents/critic.py apps/agent-worker/src/agent_worker/agents/__init__.py apps/agent-worker/tests/test_critic_agent.py
git commit -m "feat(critic): add CriticAgent"
```

---

### Task 3: Add Revision Support For BaseAgent Outputs

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/agents/base.py`
- Test: `apps/agent-worker/tests/test_base_agent_revision.py`

- [ ] **Step 1: Write failing revision test**

Create `apps/agent-worker/tests/test_base_agent_revision.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from agent_worker.agents.base import BaseAgent
from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueFinding, CritiqueReport


class _DummyAnalyzer(BaseAgent):
    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(self, kind: str, payload: dict | None = None, agent: str | None = None) -> None:
        self.events.append((kind, payload or {}, agent))


class _FakeGateway:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        yield self._chunks.pop(0)


async def test_revise_with_critique_returns_validated_output() -> None:
    revised_json = (
        '{"restated_problem":"A revised 20-character restatement.",'
        '"sub_questions":["Estimate demand","Optimize allocation"],'
        '"assumptions":["Demand observations are representative."],'
        '"data_requirements":[],'
        '"proposed_approaches":[{"name":"Robust LP","rationale":"Handles uncertainty","methods":["LP"]}]}'
    )
    agent = _DummyAnalyzer(_FakeGateway([revised_json]), _FakeEmitter())  # type: ignore[arg-type]
    original = AnalyzerOutput(
        restated_problem="A weak 20-character restatement.",
        sub_questions=["Estimate demand"],
        proposed_approaches=[ApproachSketch(name="LP", rationale="Simple", methods=["LP"])],
    )
    critique = CritiqueReport(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        passed=False,
        score=0.5,
        summary="Missing one required sub-question.",
        findings=[
            CritiqueFinding(
                severity="major",
                area="coverage",
                message="The allocation sub-question is missing.",
                evidence="sub_questions only includes demand.",
                required_change="Add allocation optimization as a sub-question.",
            )
        ],
        required_changes=["Add allocation optimization."],
    )

    revised = await agent.revise_with_critique(
        original_output=original,
        critique=critique,
        context={"problem_text": "Estimate demand and optimize allocation."},
    )

    assert isinstance(revised, AnalyzerOutput)
    assert revised.sub_questions == ["Estimate demand", "Optimize allocation"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_base_agent_revision.py -q
```

Expected: FAIL because `revise_with_critique` does not exist.

- [ ] **Step 3: Implement revision method**

In `apps/agent-worker/src/agent_worker/agents/base.py`, import `CritiqueReport`:

```python
from mm_contracts import CritiqueReport, ReasoningEffort
```

Add this method to `BaseAgent`:

```python
    async def revise_with_critique(
        self,
        *,
        original_output: BaseModel,
        critique: CritiqueReport,
        context: dict[str, Any],
    ) -> BaseModel:
        """Ask the producing agent to revise its own structured output once."""
        model = self._model_override or self.prompt.model_preference[0]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {
                "role": "user",
                "content": (
                    "Revise your previous JSON output using the Critic feedback below.\n"
                    "Return ONLY a valid JSON object matching "
                    f"{self.OUTPUT_MODEL.__name__}. Preserve correct content; change only "
                    "what is needed to satisfy the critique.\n\n"
                    f"Context JSON:\n{orjson.dumps(context).decode('utf-8')}\n\n"
                    "Original output JSON:\n"
                    f"{original_output.model_dump_json(indent=2)}\n\n"
                    "Critique JSON:\n"
                    f"{critique.model_dump_json(indent=2)}"
                ),
            },
        ]
        text = await self._stream_and_collect(model, messages)
        return self._parse_output(text)
```

- [ ] **Step 4: Run test**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_base_agent_revision.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent-worker/src/agent_worker/agents/base.py apps/agent-worker/tests/test_base_agent_revision.py
git commit -m "feat(critic): add structured output revision hook"
```

---

### Task 4: Add Pipeline Review Gate Helpers

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_pipeline_critic_gate.py`

- [ ] **Step 1: Write pure gate tests**

Create `apps/agent-worker/tests/test_pipeline_critic_gate.py`:

```python
from __future__ import annotations

from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueFinding, CritiqueReport

from agent_worker.pipeline import _critique_requires_revision, _critique_should_fail_run


def _report(*, passed: bool, severity: str | None = None) -> CritiqueReport:
    findings = []
    if severity is not None:
        findings.append(
            CritiqueFinding(
                severity=severity,  # type: ignore[arg-type]
                area="coverage",
                message="Issue.",
                evidence="Evidence.",
                required_change="Change it.",
            )
        )
    return CritiqueReport(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        passed=passed,
        score=0.6,
        summary="summary",
        findings=findings,
        required_changes=["Change it."] if findings else [],
    )


def test_passed_critique_does_not_require_revision() -> None:
    assert _critique_requires_revision(_report(passed=True)) is False


def test_major_or_blocking_critique_requires_revision() -> None:
    assert _critique_requires_revision(_report(passed=False, severity="major")) is True
    assert _critique_requires_revision(_report(passed=False, severity="blocking")) is True


def test_unresolved_blocking_critique_fails_run() -> None:
    assert _critique_should_fail_run(_report(passed=False, severity="blocking")) is True
    assert _critique_should_fail_run(_report(passed=False, severity="major")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_pipeline_critic_gate.py -q
```

Expected: FAIL because helper functions do not exist.

- [ ] **Step 3: Add helper functions**

In `apps/agent-worker/src/agent_worker/pipeline.py`, import `BaseAgent`, `CriticAgent`, and `CritiqueReport`, then add:

```python
from pydantic import BaseModel

from agent_worker.agents import BaseAgent, CriticAgent
from mm_contracts import CritiqueReport
```

Add below `_get_hmml()`:

```python
MAX_CRITIC_REVISIONS = 1


def _critique_requires_revision(report: CritiqueReport) -> bool:
    if report.passed:
        return False
    return report.has_blocking_findings or report.has_major_findings


def _critique_should_fail_run(report: CritiqueReport) -> bool:
    return (not report.passed) and report.has_blocking_findings
```

- [ ] **Step 4: Run gate tests**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_pipeline_critic_gate.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent-worker/src/agent_worker/pipeline.py apps/agent-worker/tests/test_pipeline_critic_gate.py
git commit -m "feat(critic): add pipeline critique gate helpers"
```

---

### Task 5: Integrate Analyzer And Modeler Review Gates

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_pipeline_critic_review_flow.py`

- [ ] **Step 1: Add review helper skeleton test**

Create `apps/agent-worker/tests/test_pipeline_critic_review_flow.py`:

```python
from __future__ import annotations

from typing import Any

from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueReport

from agent_worker.pipeline import _review_and_maybe_revise


class _FakeProducer:
    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput

    def __init__(self, revised: AnalyzerOutput) -> None:
        self.revised = revised
        self.revision_calls = 0

    async def revise_with_critique(
        self,
        *,
        original_output: AnalyzerOutput,
        critique: CritiqueReport,
        context: dict[str, Any],
    ) -> AnalyzerOutput:
        self.revision_calls += 1
        return self.revised


class _FakeCritic:
    def __init__(self, reports: list[CritiqueReport]) -> None:
        self.reports = reports
        self.calls = 0

    async def review(self, **_: Any) -> CritiqueReport:
        self.calls += 1
        return self.reports.pop(0)


def _analysis(sub_questions: list[str]) -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="A 20-character restatement of the problem.",
        sub_questions=sub_questions,
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Fits allocation", methods=["LP"])
        ],
    )


def _report(passed: bool) -> CritiqueReport:
    return CritiqueReport(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        passed=passed,
        score=0.9 if passed else 0.5,
        summary="ok" if passed else "needs revision",
        findings=[],
        required_changes=[],
    )


async def test_review_and_maybe_revise_returns_original_when_passed() -> None:
    original = _analysis(["Estimate demand"])
    producer = _FakeProducer(_analysis(["unused"]))
    critic = _FakeCritic([_report(True)])

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand."},
        criteria=["covers all sub-questions"],
    )

    assert result is original
    assert producer.revision_calls == 0
    assert critic.calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_pipeline_critic_review_flow.py -q
```

Expected: FAIL because `_review_and_maybe_revise` does not exist.

- [ ] **Step 3: Implement `_review_and_maybe_revise`**

In `apps/agent-worker/src/agent_worker/pipeline.py`, add:

```python
async def _review_and_maybe_revise(
    *,
    critic: CriticAgent,
    producer: BaseAgent,
    target_agent: str,
    output: BaseModel,
    context: dict[str, Any],
    criteria: list[str],
) -> BaseModel:
    report = await critic.review(
        target_agent=target_agent,  # type: ignore[arg-type]
        target_schema=type(output).__name__,
        artifact=output.model_dump(mode="json"),
        context=context,
        criteria=criteria,
    )
    if not _critique_requires_revision(report):
        return output

    revised = await producer.revise_with_critique(
        original_output=output,
        critique=report,
        context=context,
    )
    followup = await critic.review(
        target_agent=target_agent,  # type: ignore[arg-type]
        target_schema=type(revised).__name__,
        artifact=revised.model_dump(mode="json"),
        context=context,
        criteria=criteria,
    )
    if _critique_should_fail_run(followup):
        raise AgentError(
            f"Critic rejected {target_agent} after revision: {followup.summary}"
        )
    return revised
```

- [ ] **Step 4: Add failed-then-revised test**

Append to `apps/agent-worker/tests/test_pipeline_critic_review_flow.py`:

```python
async def test_review_and_maybe_revise_returns_revised_after_failed_first_review() -> None:
    original = _analysis(["Estimate demand"])
    revised = _analysis(["Estimate demand", "Optimize allocation"])
    producer = _FakeProducer(revised)
    critic = _FakeCritic([_report(False), _report(True)])

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand and optimize allocation."},
        criteria=["covers all sub-questions"],
    )

    assert result is revised
    assert producer.revision_calls == 1
    assert critic.calls == 2
```

- [ ] **Step 5: Wire Analyzer and Modeler**

In `run_pipeline`, instantiate the critic after `hmml`:

```python
critic = CriticAgent(gateway, emitter, **kwargs)
```

After Analyzer:

```python
analysis = await _review_and_maybe_revise(
    critic=critic,
    producer=analyzer,
    target_agent="analyzer",
    output=analysis,
    context={"problem_text": problem.problem_text, "competition_type": problem.competition_type},
    criteria=[
        "Restates every sub-question in the problem.",
        "Lists assumptions needed downstream.",
        "Lists concrete data requirements.",
        "Proposes at least one usable modeling approach.",
    ],
)
assert isinstance(analysis, AnalyzerOutput)
```

After Modeler:

```python
spec = await _review_and_maybe_revise(
    critic=critic,
    producer=modeler,
    target_agent="modeler",
    output=spec,
    context={
        "problem_text": problem.problem_text,
        "analysis": analysis.model_dump(mode="json"),
    },
    criteria=[
        "Chosen approach fits the analyzed problem.",
        "Variables and equations are internally consistent.",
        "Algorithm outline is executable by Coder.",
        "Validation strategy is concrete.",
    ],
)
assert isinstance(spec, ModelSpec)
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_pipeline_critic_gate.py apps/agent-worker/tests/test_pipeline_critic_review_flow.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/agent-worker/src/agent_worker/pipeline.py apps/agent-worker/tests/test_pipeline_critic_review_flow.py
git commit -m "feat(critic): gate analyzer and modeler outputs"
```

---

### Task 6: Add Coder Corrective Review Hook

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/agents/coder.py`
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: `apps/agent-worker/tests/test_coder_critic_revision.py`

- [ ] **Step 1: Write Coder critique context test**

Create `apps/agent-worker/tests/test_coder_critic_revision.py`:

```python
from __future__ import annotations

from agent_worker.agents import CoderAgent
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    CellExecution,
    CoderOutput,
    CritiqueFinding,
    CritiqueReport,
    ModelSpec,
    ProblemInput,
)


def test_coder_builds_critique_revision_problem_text() -> None:
    original = CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print('no validation')",
                stdout="no validation\n",
            )
        ],
        final_summary="Computed a baseline only.",
        notebook_path="/tmp/run/notebook.ipynb",
    )
    critique = CritiqueReport(
        target_agent="coder",
        target_schema="CoderOutput",
        passed=False,
        score=0.45,
        summary="Missing sensitivity analysis.",
        findings=[
            CritiqueFinding(
                severity="major",
                area="validation",
                message="No sensitivity or validation evidence was produced.",
                evidence="Only one baseline cell appears in CoderOutput.",
                required_change="Add sensitivity analysis and report quantitative results.",
            )
        ],
        required_changes=["Add sensitivity analysis."],
    )

    problem = ProblemInput(problem_text="Optimize allocation under uncertain demand.")
    analysis = AnalyzerOutput(
        restated_problem="Optimize allocation under uncertain demand.",
        sub_questions=["Find allocation", "Test sensitivity"],
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Fits allocation", methods=["LP"])
        ],
    )
    spec = ModelSpec(
        chosen_approach="linear programming",
        rationale="Fits constrained allocation.",
        algorithm_outline=["Solve baseline", "Run sensitivity"],
        validation_strategy="Sensitivity sweep over demand.",
    )

    revised_problem = CoderAgent.build_revision_problem(
        problem=problem,
        analysis=analysis,
        spec=spec,
        original_output=original,
        critique=critique,
    )

    assert "Critic requested one corrective Coder pass" in revised_problem.problem_text
    assert "Missing sensitivity analysis" in revised_problem.problem_text
    assert "Computed a baseline only." in revised_problem.problem_text
    assert "Optimize allocation under uncertain demand." in revised_problem.problem_text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_coder_critic_revision.py -q
```

Expected: FAIL because `build_revision_problem` does not exist.

- [ ] **Step 3: Implement Coder revision problem builder**

In `apps/agent-worker/src/agent_worker/agents/coder.py`, import `CritiqueReport` and add this static method to `CoderAgent`:

```python
    @staticmethod
    def build_revision_problem(
        *,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        spec: ModelSpec,
        original_output: CoderOutput,
        critique: CritiqueReport,
    ) -> ProblemInput:
        """Create a bounded corrective coding task from a Critic report."""
        critique_json = critique.model_dump_json(indent=2)
        cells_summary = [
            {
                "index": cell.index,
                "source": cell.source,
                "stdout": cell.stdout[:500],
                "stderr": cell.stderr[:500],
                "error": cell.error,
                "figure_paths": cell.figure_paths,
            }
            for cell in original_output.cells[-5:]
        ]
        revised_text = (
            f"{problem.problem_text}\n\n"
            "Critic requested one corrective Coder pass. Keep all valid prior "
            "results, but fix the concrete issues below. Produce a complete "
            "replacement notebook output, not a prose-only response.\n\n"
            f"Previous Coder summary:\n{original_output.final_summary}\n\n"
            f"Recent executed cells JSON:\n{json.dumps(cells_summary, ensure_ascii=False, indent=2)}\n\n"
            f"Critique JSON:\n{critique_json}"
        )
        return problem.model_copy(update={"problem_text": revised_text})
```

- [ ] **Step 4: Add Coder review function in pipeline**

In `apps/agent-worker/src/agent_worker/pipeline.py`, add a dedicated helper instead of forcing `CoderAgent` into the `BaseAgent` revision interface:

```python
async def _review_and_maybe_rerun_coder(
    *,
    critic: CriticAgent,
    coder: CoderAgent,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_out: CoderOutput,
) -> CoderOutput:
    criteria = [
        "Executed cells support the model specification.",
        "Output contains concrete numerical results.",
        "Validation or sensitivity evidence is present where applicable.",
        "Figures are registered with ids, captions, and valid paths.",
    ]
    context = {
        "problem_text": problem.problem_text,
        "analysis": analysis.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"),
    }
    report = await critic.review(
        target_agent="coder",
        target_schema="CoderOutput",
        artifact=coder_out.model_dump(mode="json"),
        context=context,
        criteria=criteria,
    )
    if not _critique_requires_revision(report):
        return coder_out

    revision_problem = CoderAgent.build_revision_problem(
        problem=problem,
        analysis=analysis,
        spec=spec,
        original_output=coder_out,
        critique=report,
    )
    revised = await coder.run(revision_problem, analysis, spec)
    followup = await critic.review(
        target_agent="coder",
        target_schema="CoderOutput",
        artifact=revised.model_dump(mode="json"),
        context=context,
        criteria=criteria,
    )
    if _critique_should_fail_run(followup):
        raise AgentError(f"Critic rejected coder after revision: {followup.summary}")
    return revised
```

- [ ] **Step 5: Add pipeline gate after Coder**

After `coder_out = await coder.run(...)`, add:

```python
coder_out = await _review_and_maybe_rerun_coder(
    critic=critic,
    coder=coder,
    problem=problem,
    analysis=analysis,
    spec=spec,
    coder_out=coder_out,
)
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_coder_agent.py apps/agent-worker/tests/test_coder_critic_revision.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/agent-worker/src/agent_worker/agents/coder.py apps/agent-worker/src/agent_worker/pipeline.py apps/agent-worker/tests/test_coder_critic_revision.py
git commit -m "feat(critic): add coder corrective review hook"
```

---

### Task 7: Add Writer Review Gate

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py`
- Test: existing writer and pipeline tests

- [ ] **Step 1: Add Writer gate in pipeline**

After Writer returns `paper`, add:

```python
paper = await _review_and_maybe_revise(
    critic=critic,
    producer=writer,
    target_agent="writer",
    output=paper,
    context={
        "problem_text": problem.problem_text,
        "competition_type": problem.competition_type,
        "analysis": analysis.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"),
        "coder_output": coder_out.model_dump(mode="json"),
        "search_findings": findings.model_dump(mode="json"),
    },
    criteria=[
        "Abstract follows award-mode numeric-result rules.",
        "Every problem sub-question is answered explicitly.",
        "Sensitivity analysis and strengths/weaknesses are present when applicable.",
        "References are sufficient and cited in the body.",
        "Figures are referenced using known figure ids and discussed with numbers.",
        "No school, student, or identifying team information appears.",
    ],
)
assert isinstance(paper, PaperDraft)
```

- [ ] **Step 2: Run writer tests**

Run:

```bash
uv run pytest apps/agent-worker/tests/test_writer_agent.py apps/agent-worker/tests/test_pipeline_critic_review_flow.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/agent-worker/src/agent_worker/pipeline.py
git commit -m "feat(critic): gate writer output"
```

---

### Task 8: Render Critique Reports In Web UI

**Files:**
- Create: `apps/web/src/components/CritiqueReport.vue`
- Modify: `apps/web/src/components/AgentOutputView.vue`
- Modify: `apps/web/src/components/StagePills.vue`
- Test: `pnpm --filter web typecheck`

- [ ] **Step 1: Create CritiqueReport component**

Create `apps/web/src/components/CritiqueReport.vue`:

```vue
<script setup lang="ts">
import type { CritiqueFinding, CritiqueReport } from "@mathodology/contracts";
import T from "./T.vue";

const props = defineProps<{ output: Record<string, unknown> }>();

const report = props.output as unknown as CritiqueReport;

function severityClass(finding: CritiqueFinding): string {
  return `sev-${finding.severity}`;
}
</script>

<template>
  <div class="output-panel critique-report">
    <div class="critique-head">
      <div>
        <div class="eyebrow">
          <T en="Critic review" zh="审查报告" />
          · {{ report.target_agent }} / {{ report.target_schema }}
        </div>
        <h3>{{ report.passed ? "Passed" : "Needs revision" }} · {{ Math.round(report.score * 100) }}%</h3>
      </div>
      <span :class="['badge', report.passed ? 'ok' : 'fail']">
        {{ report.passed ? "PASS" : "REVISE" }}
      </span>
    </div>

    <p>{{ report.summary }}</p>

    <div v-if="report.findings.length" class="findings">
      <div
        v-for="(finding, idx) in report.findings"
        :key="`${finding.area}-${idx}`"
        :class="['finding', severityClass(finding)]"
      >
        <div class="finding-title">
          <strong>{{ finding.severity }}</strong>
          <span>{{ finding.area }}</span>
        </div>
        <p>{{ finding.message }}</p>
        <p class="muted"><strong>Evidence:</strong> {{ finding.evidence }}</p>
        <p class="muted"><strong>Required change:</strong> {{ finding.required_change }}</p>
      </div>
    </div>

    <ul v-if="report.required_changes.length" class="required">
      <li v-for="change in report.required_changes" :key="change">{{ change }}</li>
    </ul>
  </div>
</template>
```

- [ ] **Step 2: Dispatch CritiqueReport schema**

In `apps/web/src/components/AgentOutputView.vue`, import and dispatch:

```ts
import CritiqueReport from "./CritiqueReport.vue";
```

Template branch:

```vue
  <component
    :is="CritiqueReport"
    v-else-if="schemaName === 'CritiqueReport'"
    :output="output"
  />
```

- [ ] **Step 3: Add Critic stage pill**

In `apps/web/src/components/StagePills.vue`, update the header comment and `AGENTS`:

```ts
const AGENTS: { agent: Exclude<AgentName, null>; num: string; en: string; zh: string }[] = [
  { agent: "analyzer", num: "A · 01", en: "Analyzer", zh: "分析员" },
  { agent: "searcher", num: "B · 02", en: "Searcher", zh: "检索员" },
  { agent: "modeler",  num: "C · 03", en: "Modeler",  zh: "建模员" },
  { agent: "coder",    num: "D · 04", en: "Coder",    zh: "编程员" },
  { agent: "writer",   num: "E · 05", en: "Writer",   zh: "撰写员" },
  { agent: "critic",   num: "Q · 06", en: "Critic",   zh: "审查员" },
];
```

- [ ] **Step 4: Add minimal styles**

Add styles to the existing component style block or `apps/web/src/styles.css`:

```css
.critique-report .critique-head {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
}

.critique-report .badge {
  border-radius: 999px;
  padding: 0.25rem 0.55rem;
  font-size: 0.75rem;
  font-weight: 700;
}

.critique-report .badge.ok {
  background: rgba(34, 197, 94, 0.14);
  color: #15803d;
}

.critique-report .badge.fail {
  background: rgba(239, 68, 68, 0.14);
  color: #b91c1c;
}

.critique-report .finding {
  border-left: 3px solid var(--line);
  padding: 0.5rem 0 0.5rem 0.75rem;
  margin: 0.75rem 0;
}

.critique-report .sev-blocking {
  border-left-color: #dc2626;
}

.critique-report .sev-major {
  border-left-color: #ea580c;
}

.critique-report .sev-minor {
  border-left-color: #ca8a04;
}
```

- [ ] **Step 5: Typecheck frontend**

Run:

```bash
pnpm --filter web typecheck
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/CritiqueReport.vue apps/web/src/components/AgentOutputView.vue apps/web/src/components/StagePills.vue apps/web/src/styles.css
git commit -m "feat(web): render critic review reports"
```

---

### Task 9: Update README Roadmap And Run Full Verification

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`

- [ ] **Step 1: Update roadmap wording**

Change Phase 4 summary from generic "In progress" to "Implemented in branch" only after all implementation tasks pass. If this is not merged yet, use:

```markdown
| **Phase 4 — Critic loop** | 🟡 In branch | [#9](https://github.com/ymylive/mathodology/issues/9) | CriticAgent · structured CritiqueReport · bounded self-refine gates |
```

Mirror the same meaning in `README_zh.md`.

- [ ] **Step 2: Run Python tests**

Run:

```bash
uv run pytest apps/agent-worker -q
```

Expected: PASS.

- [ ] **Step 3: Run Rust tests**

Run:

```bash
cargo test --workspace
```

Expected: PASS.

- [ ] **Step 4: Run frontend checks**

Run:

```bash
pnpm --filter web typecheck
pnpm --filter web build
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md README_zh.md
git commit -m "docs: update Phase 4 critic loop status"
```

---

## Self-Review Checklist

- Spec coverage: covers contracts, agent, revision, pipeline gates, UI, tests, and docs.
- No multi-round scope: revision is intentionally bounded to one pass.
- Searcher is explicitly out of first Phase 4 scope.
- Blocking failures are deterministic: unresolved blocking findings fail the run.
- UI aggregate critic stage handles multiple critic runs under one pill.
