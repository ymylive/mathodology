from __future__ import annotations

import pytest
from mm_contracts import (
    CritiqueChecklistItem,
    CritiqueFinding,
    CritiqueReport,
    RoleCritique,
)
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


def test_critique_report_accepts_role_reviews_and_checklist() -> None:
    report = CritiqueReport(
        target_agent="searcher",
        target_schema="SearchFindings",
        passed=False,
        score=0.73,
        summary="Sources are relevant but citation coverage is incomplete.",
        findings=[],
        required_changes=["Add a citable source for the optimization baseline."],
        roles=[
            RoleCritique(
                role="academic_reviewer",
                passed=False,
                score=0.73,
                summary="One claim lacks a reliable source.",
                findings=[
                    CritiqueFinding(
                        severity="major",
                        area="citation coverage",
                        message="Optimization baseline is not backed by a source.",
                        evidence="No source title mentions optimization baselines.",
                        required_change="Add one relevant paper or remove the claim.",
                    )
                ],
            )
        ],
        checklist=[
            CritiqueChecklistItem(
                id="source_quality",
                label="Sources are reliable and relevant.",
                passed=True,
                evidence="All returned sources include titles and URLs.",
            ),
            CritiqueChecklistItem(
                id="citation_coverage",
                label="Findings support downstream citations.",
                passed=False,
                evidence="The optimization baseline claim lacks support.",
            ),
        ],
        revision_round=1,
        max_revision_rounds=2,
        budget_exhausted=False,
    )

    assert report.target_agent == "searcher"
    assert report.roles[0].role == "academic_reviewer"
    assert report.has_major_findings is True
    assert report.major_finding_count == 1
    assert report.checklist_pass_rate == 0.5
    assert report.budget_exhausted is False


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
