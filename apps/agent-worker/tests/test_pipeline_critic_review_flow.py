from __future__ import annotations

from typing import Any

from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueFinding, CritiqueReport

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


def _report(passed: bool, *, major: bool = False) -> CritiqueReport:
    findings = []
    if major:
        findings.append(
            CritiqueFinding(
                severity="major",
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
        score=0.9 if passed else 0.5,
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
