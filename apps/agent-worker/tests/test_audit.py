"""Audit gate checks — drive each rule with a minimal artifact set."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_worker.audit import (
    AuditFinding,
    AuditReport,
    check_figure_glyphs_broken,
    check_figure_orphans,
    check_reference_count,
    check_subquestion_coverage,
    run_paper_audit,
)
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    CellExecution,
    CoderOutput,
    Figure,
    PaperDraft,
    PaperSection,
)

# Local alias keeps test bodies tidy.
Section = PaperSection


def _paper(
    *,
    title: str = "Test paper",
    abstract: str = "stub abstract",
    sections: list[Section] | None = None,
    references: list[str] | None = None,
) -> PaperDraft:
    return PaperDraft(
        title=title,
        abstract=abstract,
        sections=sections
        or [Section(title="1 Problem", body_markdown="problem body")],
        references=references or [],
    )


def _analysis(sub_questions: list[str]) -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="A 20-character restatement of the problem.",
        sub_questions=sub_questions,
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Fits allocation", methods=["LP"])
        ],
    )


def _coder(figures: list[Figure] | None = None) -> CoderOutput:
    return CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="x=1",
                stdout="",
                stderr="",
                figure_paths=[],
                error=None,
                duration_ms=10,
            )
        ],
        figures=figures or [],
        figure_paths=[],
        final_summary="done",
        notebook_path="notebook.ipynb",
    )


# ---------------------------------------------------------------- references


def test_check_reference_count_passes_when_above_floor(tmp_path: Path) -> None:
    refs = [f"[{i}] author N. A paper title. Journal, 202{i}." for i in range(1, 12)]
    f = check_reference_count(
        paper=_paper(references=refs),
        coder_out=_coder(),
        analysis=_analysis(["Q1"]),
        run_dir=tmp_path,
        paper_md="body",
        min_references=8,
    )
    assert f is None


def test_check_reference_count_blocks_when_below_floor(tmp_path: Path) -> None:
    f = check_reference_count(
        paper=_paper(references=["[1] X. A. Foo."]),
        coder_out=_coder(),
        analysis=_analysis(["Q1"]),
        run_dir=tmp_path,
        paper_md="body",
        min_references=8,
    )
    assert isinstance(f, AuditFinding)
    assert f.severity == "blocking"
    assert f.dispatch_to == "writer"
    assert f.code == "few_references"


# ---------------------------------------------------------------- figure orphans


def test_check_figure_orphans_passes_when_all_embedded(tmp_path: Path) -> None:
    figs = [
        Figure(id="forecast", caption="c", path_png="figures/forecast.png", width=0.8),
        Figure(id="residual", caption="c", path_png="figures/residual.png", width=0.8),
    ]
    md = (
        "body...\n"
        "![cap](figures/forecast.png)\n\n"
        "more body\n\n"
        "![cap2](figures/residual.png)\n"
    )
    assert (
        check_figure_orphans(
            paper=_paper(),
            coder_out=_coder(figures=figs),
            analysis=_analysis(["Q1"]),
            run_dir=tmp_path,
            paper_md=md,
        )
        is None
    )


def test_check_figure_orphans_blocks_when_some_missing(tmp_path: Path) -> None:
    figs = [
        Figure(id="forecast", caption="c", path_png="figures/forecast.png", width=0.8),
        Figure(id="residual", caption="c", path_png="figures/residual.png", width=0.8),
        Figure(id="tornado", caption="c", path_png="figures/tornado.png", width=0.8),
    ]
    md = "![cap](figures/forecast.png)\n"
    f = check_figure_orphans(
        paper=_paper(),
        coder_out=_coder(figures=figs),
        analysis=_analysis(["Q1"]),
        run_dir=tmp_path,
        paper_md=md,
    )
    assert isinstance(f, AuditFinding)
    assert f.severity == "blocking"
    assert f.dispatch_to == "writer"
    assert "residual" in f.message
    assert "tornado" in f.message


def test_check_figure_orphans_passes_when_no_figures(tmp_path: Path) -> None:
    assert (
        check_figure_orphans(
            paper=_paper(),
            coder_out=_coder(figures=[]),
            analysis=_analysis(["Q1"]),
            run_dir=tmp_path,
            paper_md="body",
        )
        is None
    )


# ---------------------------------------------------------------- sub-questions


def test_check_subquestion_coverage_passes_when_all_addressed(
    tmp_path: Path,
) -> None:
    md = "problem 1: estimate demand is solved. problem 2: optimize allocation."
    f = check_subquestion_coverage(
        paper=_paper(),
        coder_out=_coder(),
        analysis=_analysis(
            ["estimate demand for the store", "optimize allocation across days"]
        ),
        run_dir=tmp_path,
        paper_md=md,
    )
    assert f is None


def test_check_subquestion_coverage_blocks_when_missing(tmp_path: Path) -> None:
    md = "problem 1: estimate demand is fully solved."
    f = check_subquestion_coverage(
        paper=_paper(),
        coder_out=_coder(),
        analysis=_analysis(
            ["estimate demand for the store", "optimize allocation across days"]
        ),
        run_dir=tmp_path,
        paper_md=md,
    )
    assert isinstance(f, AuditFinding)
    assert f.severity == "blocking"
    assert f.dispatch_to == "writer"
    assert f.code == "uncovered_subquestions"


# ---------------------------------------------------------------- CJK glyphs


def test_check_figure_glyphs_returns_none_when_sentinel_missing(
    tmp_path: Path,
) -> None:
    """Pre-round-10 worker has no sentinel — we degrade silently rather
    than blocking the run on a missing signal."""
    assert (
        check_figure_glyphs_broken(
            paper=_paper(),
            coder_out=_coder(),
            analysis=_analysis(["Q1"]),
            run_dir=tmp_path,
            paper_md="body",
        )
        is None
    )


def test_check_figure_glyphs_returns_none_when_sentinel_true(tmp_path: Path) -> None:
    (tmp_path / ".cjk_font_ok").write_text("true")
    assert (
        check_figure_glyphs_broken(
            paper=_paper(),
            coder_out=_coder(),
            analysis=_analysis(["Q1"]),
            run_dir=tmp_path,
            paper_md="body",
        )
        is None
    )


def test_check_figure_glyphs_blocks_when_sentinel_false(tmp_path: Path) -> None:
    (tmp_path / ".cjk_font_ok").write_text("false")
    f = check_figure_glyphs_broken(
        paper=_paper(),
        coder_out=_coder(),
        analysis=_analysis(["Q1"]),
        run_dir=tmp_path,
        paper_md="body",
    )
    assert isinstance(f, AuditFinding)
    assert f.severity == "blocking"
    assert f.dispatch_to == "coder"  # only Coder can re-render figures
    assert f.code == "cjk_glyphs_broken"


# ---------------------------------------------------------------- end-to-end


def test_run_paper_audit_returns_no_findings_for_clean_paper(
    tmp_path: Path,
) -> None:
    (tmp_path / ".cjk_font_ok").write_text("true")
    figs = [
        Figure(id="f1", caption="c", path_png="figures/f1.png", width=0.8),
    ]
    refs = [f"[{i}] X. Y. Z. {2010 + i}." for i in range(1, 10)]
    md = (
        "intro\n\nestimate demand for the store\n\noptimize allocation\n\n"
        "![cap](figures/f1.png)\n"
    )
    report = run_paper_audit(
        paper=_paper(references=refs),
        paper_md=md,
        coder_out=_coder(figures=figs),
        analysis=_analysis(
            ["estimate demand for the store", "optimize allocation across days"]
        ),
        run_dir=tmp_path,
    )
    assert isinstance(report, AuditReport)
    assert report.passed is True
    assert report.findings == []


def test_run_paper_audit_collects_multiple_findings(tmp_path: Path) -> None:
    (tmp_path / ".cjk_font_ok").write_text("false")
    figs = [
        Figure(id="f1", caption="c", path_png="figures/f1.png", width=0.8),
        Figure(id="f2", caption="c", path_png="figures/f2.png", width=0.8),
    ]
    md = "body without subquestion keywords or fig embeds"
    report = run_paper_audit(
        paper=_paper(references=["[1] sole entry"]),
        paper_md=md,
        coder_out=_coder(figures=figs),
        analysis=_analysis(["estimate demand for store"]),
        run_dir=tmp_path,
    )
    codes = {f.code for f in report.findings}
    assert "few_references" in codes
    assert "orphan_figures" in codes
    assert "uncovered_subquestions" in codes
    assert "cjk_glyphs_broken" in codes
    assert report.passed is False
    # Dispatch routing — Writer-bound issues vs Coder-bound issue separated.
    assert any(f.dispatch_to == "coder" for f in report.findings)
    assert any(f.dispatch_to == "writer" for f in report.findings)
    # merged_hint_for(writer) bundles only writer-targeted hints.
    hint = report.merged_hint_for("writer")
    assert "audit_findings" in hint
    assert hint.count("\n-") >= 3  # at least 3 writer-targeted bullets


def test_audit_report_passed_property() -> None:
    report = AuditReport(
        findings=[
            AuditFinding(
                code="x", severity="warning", dispatch_to=None,
                message="m", fix_hint="h",
            ),
        ],
    )
    assert report.passed is True


@pytest.mark.parametrize(
    "severity,expected",
    [("blocking", False), ("warning", True)],
)
def test_audit_report_passed_only_blocks_on_blocking(
    severity: str, expected: bool,
) -> None:
    report = AuditReport(
        findings=[
            AuditFinding(
                code="x", severity=severity,  # type: ignore[arg-type]
                dispatch_to="writer", message="m", fix_hint="h",
            ),
        ],
    )
    assert report.passed is expected
