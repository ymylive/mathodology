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
