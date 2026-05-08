from __future__ import annotations

from agent_worker.pipeline import (
    CriticPolicy,
    _critique_requires_revision,
    _critique_should_fail_run,
)
from mm_contracts import CritiqueChecklistItem, CritiqueFinding, CritiqueReport


def _report(
    *,
    passed: bool,
    severity: str | None = None,
    score: float = 0.9,
    checklist_passes: list[bool] | None = None,
    budget_exhausted: bool = False,
    target_agent: str = "analyzer",
    target_schema: str = "AnalyzerOutput",
) -> CritiqueReport:
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
    checklist = [
        CritiqueChecklistItem(
            id=f"item_{idx}",
            label=f"Checklist item {idx}",
            passed=item_passed,
            evidence="Evidence.",
        )
        for idx, item_passed in enumerate(checklist_passes or [], start=1)
    ]
    return CritiqueReport(
        target_agent=target_agent,  # type: ignore[arg-type]
        target_schema=target_schema,
        passed=passed,
        score=score,
        summary="summary",
        findings=findings,
        required_changes=["Change it."] if findings else [],
        checklist=checklist,
        budget_exhausted=budget_exhausted,
    )


def test_passed_critique_does_not_require_revision() -> None:
    assert _critique_requires_revision(_report(passed=True)) is False


def test_major_or_blocking_critique_requires_revision() -> None:
    assert _critique_requires_revision(_report(passed=False, severity="major")) is True
    assert _critique_requires_revision(_report(passed=False, severity="blocking")) is True


def test_unresolved_blocking_critique_fails_run() -> None:
    assert _critique_should_fail_run(_report(passed=False, severity="blocking")) is True
    assert _critique_should_fail_run(_report(passed=False, severity="major")) is False


def test_low_score_requires_revision_even_without_findings() -> None:
    report = _report(passed=True, score=0.79)

    assert _critique_requires_revision(report, CriticPolicy(min_score=0.8)) is True


def test_low_checklist_pass_rate_requires_revision() -> None:
    report = _report(
        passed=True,
        score=0.95,
        checklist_passes=[True, True, True, True, False],
    )

    assert (
        _critique_requires_revision(report, CriticPolicy(min_checklist_pass_rate=0.85))
        is True
    )


def test_two_major_findings_require_revision_even_if_passed() -> None:
    report = _report(passed=True)
    report.findings.extend(
        [
            CritiqueFinding(
                severity="major",
                area="coverage",
                message="Missing sub-question.",
                evidence="Evidence.",
                required_change="Add it.",
            ),
            CritiqueFinding(
                severity="major",
                area="validation",
                message="Missing validation.",
                evidence="Evidence.",
                required_change="Add validation.",
            ),
        ]
    )

    assert _critique_requires_revision(report) is True


def test_budget_exhaustion_with_blocking_fails_run() -> None:
    report = _report(
        passed=False,
        severity="blocking",
        score=0.5,
        budget_exhausted=True,
    )

    assert _critique_should_fail_run(report) is True


def test_default_policy_has_active_revision_cost_estimates() -> None:
    policy = CriticPolicy()

    assert policy.estimated_review_cost_rmb > 0
    assert policy.estimated_revision_cost_rmb > 0
    assert policy.estimated_coder_revision_cost_rmb > policy.estimated_revision_cost_rmb


# checklist_pass_rate = 21/25 = 0.84 (between the 0.80 searcher override and
# the 0.85 uniform default), score 0.78 (between the 0.75 override and the
# 0.80 default). Both signals cross the override threshold but not the default.
# Capped at 25 items because CritiqueReport.checklist has max_length=30.
_SEARCHER_OVERRIDE_PASSES = [True] * 21 + [False] * 4


def test_searcher_below_uniform_threshold_passes_with_override() -> None:
    report = _report(
        passed=True,
        score=0.78,
        checklist_passes=_SEARCHER_OVERRIDE_PASSES,
        target_agent="searcher",
        target_schema="SearchFindings",
    )

    assert _critique_requires_revision(report, CriticPolicy()) is False


def test_modeler_below_uniform_threshold_still_requires_revision() -> None:
    report = _report(
        passed=True,
        score=0.78,
        checklist_passes=_SEARCHER_OVERRIDE_PASSES,
        target_agent="modeler",
        target_schema="ModelSpec",
    )

    assert _critique_requires_revision(report, CriticPolicy()) is True


def test_explicit_override_can_be_disabled_per_call() -> None:
    report = _report(
        passed=True,
        score=0.78,
        checklist_passes=_SEARCHER_OVERRIDE_PASSES,
        target_agent="searcher",
        target_schema="SearchFindings",
    )

    policy = CriticPolicy(
        min_score_overrides={},
        min_checklist_pass_rate_overrides={},
    )

    assert _critique_requires_revision(report, policy) is True
