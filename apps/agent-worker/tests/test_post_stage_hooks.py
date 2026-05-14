"""Tests for the post-stage hook contract.

Mirrors claude-code-sourcemap's `syncHookResponseSchema` discriminated
union (`types/hooks.ts:50-166`) and dispatch aggregation
(`utils/hooks.ts:550-660`).
"""

from __future__ import annotations

from agent_worker.agents.hooks import (
    AggregatedHookResult,
    HookResult,
    PostStageOutput,
    aggregate,
    critique_to_hook_result,
)
from mm_contracts import CritiqueChecklistItem, CritiqueFinding, CritiqueReport, RoleCritique


def _approve(ctx: str | None = None) -> HookResult:
    return HookResult(
        decision="approve",
        hook_specific_output=PostStageOutput(additional_context=ctx),
    )


def _block(reason: str, ctx: str | None = None) -> HookResult:
    return HookResult(
        decision="block",
        reason=reason,
        hook_specific_output=PostStageOutput(additional_context=ctx),
    )


def _report(*, summary: str, findings: list[CritiqueFinding]) -> CritiqueReport:
    return CritiqueReport(
        target_agent="writer",
        target_schema="PaperDraft",
        passed=not any(f.severity == "blocking" for f in findings),
        score=0.85,
        summary=summary,
        findings=findings,
        required_changes=[],
        roles=[
            RoleCritique(
                role="academic_reviewer",
                passed=True,
                score=0.85,
                summary="ok",
                findings=[],
            )
        ],
        checklist=[
            CritiqueChecklistItem(
                id="award_abstract", label="abstract", passed=True, evidence="ok"
            )
        ],
        revision_round=0,
        max_revision_rounds=2,
        budget_exhausted=False,
    )


# --- aggregate ---------------------------------------------------------------


def test_aggregate_empty_list_yields_empty_result() -> None:
    agg = aggregate([])
    assert agg.blocking_errors == []
    assert agg.additional_contexts == []
    assert agg.merged_reminder() == ""
    assert not agg.should_block


def test_aggregate_collects_block_reasons() -> None:
    agg = aggregate([_approve(), _block("missing sensitivity"), _block("anonymity leak")])
    assert agg.should_block
    assert "missing sensitivity" in agg.blocking_errors
    assert "anonymity leak" in agg.blocking_errors


def test_aggregate_collects_additional_contexts() -> None:
    agg = aggregate(
        [
            _approve("minor: tighten abstract by 50 words"),
            _approve("minor: fix DOI [4]"),
        ]
    )
    assert agg.additional_contexts == [
        "minor: tighten abstract by 50 words",
        "minor: fix DOI [4]",
    ]
    reminder = agg.merged_reminder()
    assert reminder.startswith("<system_reminder>")
    assert reminder.endswith("</system_reminder>")
    assert "minor: tighten abstract" in reminder
    assert "minor: fix DOI" in reminder


def test_aggregate_filters_empty_or_whitespace_contexts() -> None:
    agg = aggregate(
        [
            _approve(""),
            _approve("   "),
            _approve(None),
            _approve("real reminder"),
        ]
    )
    assert agg.additional_contexts == ["real reminder"]
    assert "real reminder" in agg.merged_reminder()


def test_aggregate_last_updated_artifact_wins() -> None:
    r1 = HookResult(
        decision="approve",
        hook_specific_output=PostStageOutput(updated_artifact={"v": 1}),
    )
    r2 = HookResult(
        decision="approve",
        hook_specific_output=PostStageOutput(updated_artifact={"v": 2}),
    )
    agg = aggregate([r1, r2])
    assert agg.updated_artifact == {"v": 2}


# --- critique_to_hook_result -------------------------------------------------


def test_critique_to_hook_result_passing_report_approves() -> None:
    rep = _report(summary="all good", findings=[])
    h = critique_to_hook_result(rep)
    assert h.decision == "approve"
    # No reminders when no minor findings AND empty summary handling
    assert h.hook_specific_output is not None
    # summary IS non-empty, so the reminder includes it
    assert h.hook_specific_output.additional_context
    assert "all good" in h.hook_specific_output.additional_context


def test_critique_to_hook_result_with_2plus_majors_blocks() -> None:
    findings = [
        CritiqueFinding(
            severity="major",
            area="equation_consistency",
            message="eq 4 has dimensional mismatch",
            evidence="kg/s vs kg", required_change="rebalance",
        ),
        CritiqueFinding(
            severity="major",
            area="reproducibility",
            message="seed not declared",
            evidence="np.random.normal() at line 12", required_change="add np.random.seed",
        ),
    ]
    rep = _report(summary="2 major issues", findings=findings)
    h = critique_to_hook_result(rep)
    assert h.decision == "block"
    assert h.reason == "2 major issues"


def test_critique_to_hook_result_with_blocking_blocks() -> None:
    findings = [
        CritiqueFinding(
            severity="blocking",
            area="anonymity",
            message="school name in abstract",
            evidence="...", required_change="anonymize",
        )
    ]
    rep = _report(summary="DQ risk", findings=findings)
    h = critique_to_hook_result(rep)
    assert h.decision == "block"


def test_critique_to_hook_result_minor_findings_become_reminder_bullets() -> None:
    findings = [
        CritiqueFinding(
            severity="minor",
            area="exposition",
            message="abstract is long; consider tightening",
            evidence="603 words", required_change="trim",
        ),
        CritiqueFinding(
            severity="info",
            area="references",
            message="[7] could include the URL access date",
            evidence="ref list", required_change="add URL",
        ),
    ]
    rep = _report(summary="approved with minors", findings=findings)
    h = critique_to_hook_result(rep)
    assert h.decision == "approve"
    ctx = h.hook_specific_output.additional_context
    assert ctx is not None
    assert "abstract is long" in ctx
    assert "URL access date" in ctx


def test_critique_to_hook_result_caps_at_5_minor_findings() -> None:
    findings = [
        CritiqueFinding(
            severity="minor",
            area="x",
            message=f"minor #{i}",
            evidence="e",
            required_change="fix",
        )
        for i in range(10)
    ]
    rep = _report(summary="ok", findings=findings)
    h = critique_to_hook_result(rep)
    ctx = h.hook_specific_output.additional_context
    # Exactly 5 of the 10 minors are rendered (sourcemap convention)
    assert ctx.count("minor #") == 5


def test_aggregated_result_merged_reminder_xml_block() -> None:
    rep = _report(
        summary="approved",
        findings=[
            CritiqueFinding(severity="minor", area="x", message="msg one", evidence="e", required_change="fix"),
        ],
    )
    agg = aggregate([critique_to_hook_result(rep)])
    rem = agg.merged_reminder()
    assert "<system_reminder>" in rem
    assert "msg one" in rem


def test_post_stage_output_extra_forbidden() -> None:
    """Schema should reject unknown keys (matches sourcemap's strict Zod schemas)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PostStageOutput(unknown_field="x")  # type: ignore[call-arg]


def test_hook_result_extra_forbidden() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        HookResult(decision="approve", unknown="oops")  # type: ignore[call-arg]


def test_aggregated_result_extra_forbidden() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AggregatedHookResult(blocking_errors=[], unknown="oops")  # type: ignore[call-arg]
