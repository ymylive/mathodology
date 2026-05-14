from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from agent_worker.agents import AgentError
from agent_worker.pipeline import (
    CriticPolicy,
    _review_and_maybe_revise,
    _searcher_review_criteria,
)
from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueFinding, CritiqueReport

# Round-7: the default CriticPolicy caps analyzer at 1 revision round. These
# flow tests target_agent="analyzer" but are testing the policy MECHANISM
# (does the loop honor max_revision_rounds=2?), not the analyzer-specific
# cap, so we disable the per-stage overrides for them.
_NO_OVERRIDES: MappingProxyType[str, int] = MappingProxyType({})


class _FakeProducer:
    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput

    def __init__(self, revised: AnalyzerOutput) -> None:
        self.revisions = [revised]
        self.revision_calls = 0

    async def revise_with_critique(
        self,
        *,
        original_output: AnalyzerOutput,
        critique: CritiqueReport,
        context: dict[str, Any],
    ) -> AnalyzerOutput:
        self.revision_calls += 1
        return self.revisions[min(self.revision_calls - 1, len(self.revisions) - 1)]


class _SequenceProducer(_FakeProducer):
    def __init__(self, revisions: list[AnalyzerOutput]) -> None:
        super().__init__(revisions[0])
        self.revisions = revisions


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


def _report(
    passed: bool,
    *,
    major: bool = False,
    blocking: bool = False,
    score: float | None = None,
) -> CritiqueReport:
    findings = []
    if major or blocking:
        findings.append(
            CritiqueFinding(
                severity="blocking" if blocking else "major",
                area="coverage",
                message="Missing allocation sub-question.",
                evidence="Only demand is listed.",
                required_change="Add allocation optimization.",
            )
        )
    return CritiqueReport(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        passed=passed,
        score=score if score is not None else (0.9 if passed else 0.5),
        summary="ok" if passed else "needs revision",
        findings=findings,
        required_changes=["Add allocation optimization."] if findings else [],
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


async def test_review_and_maybe_revise_returns_revised_after_failed_first_review() -> None:
    original = _analysis(["Estimate demand"])
    revised = _analysis(["Estimate demand", "Optimize allocation"])
    producer = _FakeProducer(revised)
    critic = _FakeCritic([_report(False, major=True), _report(True)])

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


async def test_review_and_maybe_revise_allows_two_revision_rounds() -> None:
    original = _analysis(["Estimate demand"])
    first_revision = _analysis(["Estimate demand", "Optimize allocation"])
    second_revision = _analysis(
        ["Estimate demand", "Optimize allocation", "Validate robustness"]
    )
    producer = _SequenceProducer([first_revision, second_revision])
    critic = _FakeCritic(
        [
            _report(False, major=True),
            _report(True, score=0.79),
            _report(True, score=0.92),
        ]
    )

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand and optimize allocation."},
        criteria=["covers all sub-questions"],
        policy=CriticPolicy(
            max_revision_rounds=2,
            max_revision_rounds_overrides=_NO_OVERRIDES,
            max_revision_cost_rmb=10.0,
        ),
    )

    assert result is second_revision
    assert producer.revision_calls == 2
    assert critic.calls == 3


def test_searcher_review_criteria_cover_source_quality_and_empty_results() -> None:
    criteria = _searcher_review_criteria()

    joined = "\n".join(criteria).lower()
    assert "source" in joined
    assert "citation" in joined
    assert "relevance" in joined
    assert "empty" in joined


async def test_review_and_maybe_revise_stops_when_cost_budget_is_exhausted() -> None:
    original = _analysis(["Estimate demand"])
    producer = _SequenceProducer(
        [
            _analysis(["Estimate demand", "Optimize allocation"]),
            _analysis(["Estimate demand", "Optimize allocation", "Validate robustness"]),
        ]
    )
    critic = _FakeCritic(
        [
            _report(False, major=True),
            _report(False, major=True),
        ]
    )

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand and optimize allocation."},
        criteria=["covers all sub-questions"],
        policy=CriticPolicy(
            max_revision_rounds=2,
            max_revision_cost_rmb=0.04,
            max_revision_rounds_overrides=_NO_OVERRIDES,
        ),
        estimated_review_cost_rmb=0.01,
        estimated_revision_cost_rmb=0.02,
    )

    assert result is producer.revisions[0]
    assert producer.revision_calls == 1
    assert critic.calls == 2


async def test_review_and_maybe_revise_fails_after_budget_with_blocking() -> None:
    original = _analysis(["Estimate demand"])
    producer = _SequenceProducer(
        [
            _analysis(["Estimate demand", "Optimize allocation"]),
            _analysis(["Estimate demand", "Optimize allocation", "Validate robustness"]),
        ]
    )
    critic = _FakeCritic(
        [
            _report(False, blocking=True),
            _report(False, blocking=True),
            _report(False, blocking=True),
        ]
    )

    with pytest.raises(AgentError):
        await _review_and_maybe_revise(
            critic=critic,  # type: ignore[arg-type]
            producer=producer,  # type: ignore[arg-type]
            target_agent="analyzer",
            output=original,
            context={"problem_text": "Estimate demand and optimize allocation."},
            criteria=["covers all sub-questions"],
            policy=CriticPolicy(
                max_revision_rounds=2,
                max_revision_rounds_overrides=_NO_OVERRIDES,
                max_revision_cost_rmb=10.0,
            ),
        )

    assert producer.revision_calls == 2
    assert critic.calls == 3
