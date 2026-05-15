"""Pre-submission paper audit gate.

After the Writer (and its Critic pass) finishes, the pipeline used to
ship the paper immediately. Real runs surfaced silent quality failures
that none of the per-stage Critics caught — figures rendering as Unicode
boxes because matplotlib fell back to a non-CJK font, orphan figures
that exist on disk but never appear in the markdown, papers shipping
with 3 references instead of 15. By the time the user notices, the run
has cost ¥4-5 and they have to start over.

This module adds a final review gate. ``run_paper_audit`` runs a list of
deterministic checks against the finished artifacts (paper.md /
paper.meta.json / figures/ / CoderOutput / AnalyzerOutput). Each finding
carries a `dispatch_to` hint pointing to the agent best-equipped to fix
it; ``pipeline.py`` consults the report to decide whether to revise or
ship.

Checks are intentionally simple and rule-based (file globs, regex,
counts) — anything that needs an LLM verdict already runs in the per-
stage Critic. Add new checks by writing a function that returns
``AuditFinding | None`` and registering it in ``_CHECKS``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mm_contracts import (
    AnalyzerOutput,
    CoderOutput,
    PaperDraft,
)

_log = logging.getLogger(__name__)

# Agents the gate is allowed to bounce work back to. Keep this aligned
# with `_STAGE_ORDER` in pipeline.py — adding a new value here requires
# wiring a corresponding revision path on the pipeline side.
DispatchTarget = Literal["coder", "writer"]
Severity = Literal["blocking", "warning"]


@dataclass(frozen=True)
class AuditFinding:
    """One issue uncovered by an audit check.

    `code` is a short stable identifier used by tests and by the
    dispatch logic in pipeline.py. `message` is the human-readable
    summary surfaced as a `log` event. `fix_hint` is appended to the
    target agent's revision instructions so the LLM knows exactly what
    to address.
    """

    code: str
    severity: Severity
    dispatch_to: DispatchTarget | None
    message: str
    fix_hint: str


@dataclass(frozen=True)
class AuditReport:
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """No blocking findings — the paper is OK to ship."""
        return not any(f.severity == "blocking" for f in self.findings)

    def blocking_for(self, agent: DispatchTarget) -> list[AuditFinding]:
        return [
            f
            for f in self.findings
            if f.severity == "blocking" and f.dispatch_to == agent
        ]

    def merged_hint_for(self, agent: DispatchTarget) -> str:
        """Concatenate all blocking fix_hints targeted at this agent."""
        hints = [f.fix_hint for f in self.blocking_for(agent)]
        if not hints:
            return ""
        bullets = "\n".join(f"- {h}" for h in hints)
        return (
            "<audit_findings>\n"
            f"The paper audit found these issues you must fix:\n{bullets}\n"
            "</audit_findings>"
        )


# ============================================================================
# Individual checks
# ============================================================================
#
# Each check has the signature:
#   def check_xxx(*, paper, coder_out, analysis, run_dir) -> AuditFinding | None
# Return None when everything's fine.
# ============================================================================


_FIG_PLACEHOLDER_RE = re.compile(r"\[\[FIG:([^\]]+)\]\]")
_FIG_EMBED_RE = re.compile(r"!\[[^\]]*\]\(figures/([^)]+)\)")


def check_reference_count(
    *,
    paper: PaperDraft,
    coder_out: CoderOutput,
    analysis: AnalyzerOutput,
    run_dir: Path,
    paper_md: str,
    min_references: int = 8,
) -> AuditFinding | None:
    """Outstanding-tier MCM/CUMCM papers cite 15+ sources; a hard floor of 8
    here catches the case where Searcher returned little, Writer dropped
    most, and the paper ends up with 2-3 inline citations to nothing.
    """
    n = len(paper.references)
    if n >= min_references:
        return None
    return AuditFinding(
        code="few_references",
        severity="blocking",
        dispatch_to="writer",
        message=f"paper has only {n} reference(s); minimum threshold is {min_references}",
        fix_hint=(
            f"The paper currently lists {n} references but a CUMCM/MCM paper "
            f"of this length must cite at least {min_references}. Pull more "
            "candidates from coder context / SearchFindings, weave them into "
            "the prose with [N] inline markers, and add full bibliographic "
            "entries to the references list. Do NOT invent citations."
        ),
    )


def check_figure_orphans(
    *,
    paper: PaperDraft,
    coder_out: CoderOutput,
    analysis: AnalyzerOutput,
    run_dir: Path,
    paper_md: str,
) -> AuditFinding | None:
    """Coder shipped figures; Writer is supposed to embed every one with a
    `[[FIG:<id>]]` placeholder. Anything in `coder_out.figures` that
    doesn't appear in the rendered markdown is wasted Coder work and a
    scoring deduction.

    We check against the RENDERED markdown (post-substitution) rather
    than the meta json, because that's what reviewers see.
    """
    if not coder_out.figures:
        return None
    embedded_ids = set(_FIG_EMBED_RE.findall(paper_md))
    # Embed path looks like "forecast_week9_interval_corrected.png" — strip
    # the suffix to compare against figure ids.
    embedded_ids = {Path(p).stem for p in embedded_ids}
    declared_ids = {f.id for f in coder_out.figures}
    orphans = declared_ids - embedded_ids
    if not orphans:
        return None
    orphan_list = ", ".join(sorted(orphans))
    return AuditFinding(
        code="orphan_figures",
        severity="blocking",
        dispatch_to="writer",
        message=(
            f"{len(orphans)} of {len(declared_ids)} coder figures are not "
            f"embedded in paper.md: {orphan_list}"
        ),
        fix_hint=(
            f"Coder produced {len(declared_ids)} figures but only "
            f"{len(declared_ids) - len(orphans)} are referenced in your "
            "sections. Add `[[FIG:<id>]]` placeholders for the orphan "
            f"figures ({orphan_list}) in the sections where they best "
            "support the prose. Every embedded figure must have at least "
            "one sentence of surrounding discussion."
        ),
    )


def check_subquestion_coverage(
    *,
    paper: PaperDraft,
    coder_out: CoderOutput,
    analysis: AnalyzerOutput,
    run_dir: Path,
    paper_md: str,
) -> AuditFinding | None:
    """Every sub-question that the Analyzer identified must be visibly
    addressed in the paper. We do a substring match against a short
    distinctive phrase from each sub_question (first 10 chars, stripped
    of punctuation). Missed sub-questions are a top scoring concern in
    MCM/CUMCM rubrics.
    """
    if not analysis.sub_questions:
        return None
    md_lower = paper_md.lower()
    missing: list[str] = []
    for q in analysis.sub_questions:
        # Strip leading "问题N：" or "Q: " prefixes that won't appear in body.
        cleaned = re.sub(r"^[\s问题题目QqＱ]+[:：\d.0-9、\-\s]*", "", q).strip()
        if not cleaned:
            continue
        # Use first ~12 characters; long enough to be distinctive, short
        # enough to survive paraphrasing in the body.
        key = cleaned[:12].lower()
        if key and key not in md_lower:
            missing.append(q[:60])
    if not missing:
        return None
    bullet_list = "\n".join(f"  - {q}" for q in missing)
    return AuditFinding(
        code="uncovered_subquestions",
        severity="blocking",
        dispatch_to="writer",
        message=f"{len(missing)} sub-question(s) appear unaddressed in the paper body",
        fix_hint=(
            "The Analyzer surfaced these sub-questions but they don't "
            f"appear (even paraphrased) in the rendered paper:\n{bullet_list}\n"
            "Each one must be answered explicitly — either in §1 (问题重述) "
            "or in a dedicated subsection — using concrete numbers from the "
            "Coder's results."
        ),
    )


def check_figure_glyphs_broken(
    *,
    paper: PaperDraft,
    coder_out: CoderOutput,
    analysis: AnalyzerOutput,
    run_dir: Path,
    paper_md: str,
) -> AuditFinding | None:
    """Detect the "Chinese title rendered as □□□□" failure mode.

    The kernel bootstrap announces `cjk_font_ok` via a log event on its
    first cell (see ``_MPL_BOOTSTRAP_SRC`` in kernel/manager.py). If
    the marker is missing OR is false, every figure that contains CJK
    text is broken — title and axis labels will be Unicode replacement
    boxes in the rendered PDF.

    Fix path: bounce back to Coder with an instruction to either (a)
    skip CJK in figure text (use English labels) or (b) explicitly load
    a known-good font. We don't try to do this auto-fix here — Coder is
    the agent that ran the cell, so it owns the remediation.
    """
    sentinel = run_dir / ".cjk_font_ok"
    if not sentinel.is_file():
        # Either the kernel never started, or we're running against a
        # worker built before the bootstrap was extended to write this
        # file. Give the run the benefit of the doubt — this check is a
        # confirmation, not the only quality signal.
        return None
    try:
        content = sentinel.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if content == "true":
        return None
    if content != "false":
        # Unexpected payload — don't block, just skip.
        return None
    return AuditFinding(
        code="cjk_glyphs_broken",
        severity="blocking",
        dispatch_to="coder",
        message=(
            "matplotlib could not resolve a CJK font; Chinese text in "
            "figures will render as Unicode replacement boxes"
        ),
        fix_hint=(
            "Your matplotlib figures cannot render Chinese characters — the "
            "kernel's CJK font lookup failed and every title / axis label / "
            "legend containing Chinese will be displayed as □ boxes in the "
            "exported PDF. Regenerate the figures with ENGLISH labels for "
            "titles, axis names, and legend entries. Keep the surrounding "
            "paper prose in Chinese; only the rendered figures need ASCII."
        ),
    )


_CHECKS: list[Callable[..., AuditFinding | None]] = [
    check_reference_count,
    check_figure_orphans,
    check_subquestion_coverage,
    check_figure_glyphs_broken,
]


def run_paper_audit(
    *,
    paper: PaperDraft,
    paper_md: str,
    coder_out: CoderOutput,
    analysis: AnalyzerOutput,
    run_dir: Path,
) -> AuditReport:
    """Run every registered check and return a combined report.

    Checks that raise (a bug in the check itself) are logged and
    skipped so a single broken check can't block the run. The default
    is to ship — only explicit blocking findings can stop submission.
    """
    findings: list[AuditFinding] = []
    for check in _CHECKS:
        try:
            f = check(
                paper=paper,
                coder_out=coder_out,
                analysis=analysis,
                run_dir=run_dir,
                paper_md=paper_md,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never fail the run
            _log.warning("audit check %s raised %s; skipping", check.__name__, exc)
            continue
        if f is not None:
            findings.append(f)
    return AuditReport(findings=findings)


__all__ = [
    "AuditFinding",
    "AuditReport",
    "DispatchTarget",
    "Severity",
    "run_paper_audit",
]
