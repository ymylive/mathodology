from __future__ import annotations

from mm_contracts import CritiqueFinding, CritiqueReport

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
