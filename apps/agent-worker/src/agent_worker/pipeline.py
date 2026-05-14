"""Pipeline: Analyzer → Searcher → Modeler → Coder → Writer (M10, 5 agents).

The `done` event carries both `notebook_path` and `paper_path` so the
gateway's audit task can persist them and the UI can offer downloads.

M9 adds the HMML knowledge base: the Modeler consults a BM25-indexed library
of ~30 canonical modeling methods before producing its ModelSpec. The service
is loaded lazily once per process; if the seed dir is missing or empty the
Modeler transparently falls back to its pre-M9 behavior.

M10 inserts the Searcher between Analyzer and Modeler: it derives queries from
the Analyzer output, hits arXiv for prior work, and passes curated findings to
the Writer for Related Work / References. The Modeler is NOT affected (HMML
remains its only external context). If arXiv is unreachable the Searcher
degrades to an empty SearchFindings and the pipeline continues.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

from mm_contracts import (
    AnalyzerOutput,
    CoderOutput,
    CritiqueReport,
    Figure,
    ModelSpec,
    PaperDraft,
    ProblemInput,
    SearchFindings,
)
from pydantic import BaseModel
from redis.asyncio import Redis

from agent_worker.agents import (
    AgentError,
    AnalyzerAgent,
    BaseAgent,
    CoderAgent,
    CriticAgent,
    ModelerAgent,
    SearcherAgent,
    WriterAgent,
)
from agent_worker.agents.evidence import (
    anonymity_criteria,
    mine_sensitivity_evidence,
    scan_anonymity_violations,
    sensitivity_criteria,
)
from agent_worker.agents.hooks import aggregate, critique_to_hook_result
from agent_worker.config import get_settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.hmml import HMMLService
from agent_worker.kernel import KernelSession
from agent_worker.matlab import MatlabSession

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticPolicy:
    min_score: float = 0.80
    min_checklist_pass_rate: float = 0.85
    max_revision_rounds: int = 2
    coder_revision_iterations: int = 2
    max_revision_cost_rmb: float = 1.00
    estimated_review_cost_rmb: float = 0.02
    estimated_revision_cost_rmb: float = 0.05
    estimated_coder_revision_cost_rmb: float = 0.12
    min_score_overrides: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({"searcher": 0.75})
    )
    min_checklist_pass_rate_overrides: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({"searcher": 0.80})
    )


DEFAULT_CRITIC_POLICY = CriticPolicy()


@lru_cache(maxsize=1)
def _get_hmml() -> HMMLService | None:
    """Load the HMML service once per process. Degrade to None on empty seed dir."""
    try:
        service = HMMLService.from_seed_dir()
    except Exception as e:  # noqa: BLE001 — any seed-load failure is non-fatal
        _log.warning("HMML seed load failed; Modeler will run without it: %s", e)
        return None
    if not service.methods:
        _log.warning("HMML seed dir is empty; Modeler will run without it.")
        return None
    return service


def _critique_requires_revision(
    report: CritiqueReport, policy: CriticPolicy = DEFAULT_CRITIC_POLICY
) -> bool:
    if report.budget_exhausted:
        return False
    if report.has_blocking_findings:
        return True
    if report.major_finding_count >= 2:
        return True
    min_score = policy.min_score_overrides.get(
        report.target_agent, policy.min_score
    )
    if report.score < min_score:
        return True
    min_pass_rate = policy.min_checklist_pass_rate_overrides.get(
        report.target_agent, policy.min_checklist_pass_rate
    )
    if report.checklist_pass_rate < min_pass_rate:
        return True
    if report.passed:
        return False
    return report.has_major_findings


def _critique_should_fail_run(report: CritiqueReport) -> bool:
    if report.has_blocking_findings:
        return (not report.passed) or report.budget_exhausted
    return False


_STAGE_ORDER = ["analyzer", "searcher", "modeler", "coder", "writer"]


def _next_stage_after(stage: str) -> str | None:
    """Return the stage name that consumes `stage`'s output, or None for writer."""
    try:
        i = _STAGE_ORDER.index(stage)
    except ValueError:
        return None
    return _STAGE_ORDER[i + 1] if i + 1 < len(_STAGE_ORDER) else None


def _searcher_review_criteria() -> list[str]:
    return [
        "Source quality is reliable enough for academic citation or clearly marked as web context.",
        "Citation coverage supports every synthesized key finding that downstream Writer may cite.",
        "Source relevance is clear for the analyzed modeling approach and problem context.",
        "Empty or sparse search results are handled gracefully without inventing references.",
    ]


async def _review_and_maybe_revise(
    *,
    critic: CriticAgent,
    producer: BaseAgent,
    target_agent: str,
    output: BaseModel,
    context: dict[str, Any],
    criteria: list[str],
    policy: CriticPolicy = DEFAULT_CRITIC_POLICY,
    estimated_review_cost_rmb: float | None = None,
    estimated_revision_cost_rmb: float | None = None,
) -> BaseModel:
    review_cost_rmb = (
        policy.estimated_review_cost_rmb
        if estimated_review_cost_rmb is None
        else estimated_review_cost_rmb
    )
    revision_cost_rmb = (
        policy.estimated_revision_cost_rmb
        if estimated_revision_cost_rmb is None
        else estimated_revision_cost_rmb
    )
    current = output
    loop_cost_rmb = 0.0
    report = await critic.review(
        target_agent=target_agent,  # type: ignore[arg-type]
        target_schema=type(current).__name__,
        artifact=current.model_dump(mode="json"),
        context=context,
        criteria=criteria,
        revision_round=0,
        max_revision_rounds=policy.max_revision_rounds,
    )
    loop_cost_rmb += review_cost_rmb
    if not _critique_requires_revision(report, policy):
        return current

    for revision_round in range(1, policy.max_revision_rounds + 1):
        if loop_cost_rmb + revision_cost_rmb > policy.max_revision_cost_rmb:
            report.budget_exhausted = True
            break
        try:
            current = await producer.revise_with_critique(
                original_output=current,
                critique=report,
                context=context,
            )
        except AgentError as exc:
            # Revision call failed (transient network / parse). Don't sink the
            # whole run — keep the last valid `current` and abandon revision.
            _log.warning(
                "revise_with_critique for %s failed at round %d; keeping prior output: %s",
                target_agent,
                revision_round,
                exc,
            )
            break
        loop_cost_rmb += revision_cost_rmb
        if loop_cost_rmb + review_cost_rmb > policy.max_revision_cost_rmb:
            report.budget_exhausted = True
            break
        report = await critic.review(
            target_agent=target_agent,  # type: ignore[arg-type]
            target_schema=type(current).__name__,
            artifact=current.model_dump(mode="json"),
            context=context,
            criteria=criteria,
            revision_round=revision_round,
            max_revision_rounds=policy.max_revision_rounds,
        )
        loop_cost_rmb += review_cost_rmb
        if not _critique_requires_revision(report, policy):
            return current

    if _critique_should_fail_run(report):
        raise AgentError(
            f"Critic rejected {target_agent} after revision: {report.summary}"
        )

    # Post-stage hook lifecycle (borrowed from claude-code-sourcemap
    # types/hooks.ts:50-166). Convert the final Critic verdict into a
    # HookResult and surface any `additional_context` as a structured
    # `log` event tagged for the NEXT stage. Future iterations can read
    # this reminder back from events.jsonl and render it as a
    # `<system_reminder>` block in the downstream prompt; for this round
    # we only persist the signal so post-mortems carry it forward.
    hook_result = critique_to_hook_result(report)
    agg = aggregate([hook_result])
    reminder = agg.merged_reminder()
    if reminder:
        import contextlib

        # Critic exposes `emitter` on all current builds; suppress just in
        # case a future refactor changes the surface — we never want the
        # hook to be the reason a run fails.
        with contextlib.suppress(AttributeError):
            await critic.emitter.emit(  # type: ignore[attr-defined]
                "log",
                {
                    "level": "info",
                    "message": "post-stage reminder available for downstream",
                    "for_stage": _next_stage_after(target_agent),
                    "reminder_chars": len(reminder),
                    "reminder": reminder,
                },
                agent=target_agent,
            )
    return current


async def _review_and_maybe_rerun_coder(
    *,
    critic: CriticAgent,
    coder: CoderAgent,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_out: CoderOutput,
    policy: CriticPolicy = DEFAULT_CRITIC_POLICY,
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
        revision_round=0,
        max_revision_rounds=policy.max_revision_rounds,
    )
    loop_cost_rmb = policy.estimated_review_cost_rmb
    if not _critique_requires_revision(report, policy):
        return coder_out

    current = coder_out
    for revision_round in range(1, policy.max_revision_rounds + 1):
        if (
            loop_cost_rmb + policy.estimated_coder_revision_cost_rmb
            > policy.max_revision_cost_rmb
        ):
            report.budget_exhausted = True
            break
        revision_problem = CoderAgent.build_revision_problem(
            problem=problem,
            analysis=analysis,
            spec=spec,
            original_output=current,
            critique=report,
        )
        try:
            current = await coder.run(
                revision_problem,
                analysis,
                spec,
                max_iterations=policy.coder_revision_iterations,
            )
        except AgentError as exc:
            # Coder rerun crashed (often upstream LLM disconnect). The prior
            # `current` already contains a notebook + figures, so it's strictly
            # better to ship that than to fail the whole run.
            _log.warning(
                "coder rerun round %d failed; keeping prior CoderOutput: %s",
                revision_round,
                exc,
            )
            break
        loop_cost_rmb += policy.estimated_coder_revision_cost_rmb
        if (
            loop_cost_rmb + policy.estimated_review_cost_rmb
            > policy.max_revision_cost_rmb
        ):
            report.budget_exhausted = True
            break
        report = await critic.review(
            target_agent="coder",
            target_schema="CoderOutput",
            artifact=current.model_dump(mode="json"),
            context=context,
            criteria=criteria,
            revision_round=revision_round,
            max_revision_rounds=policy.max_revision_rounds,
        )
        loop_cost_rmb += policy.estimated_review_cost_rmb
        if not _critique_requires_revision(report, policy):
            return current

    if _critique_should_fail_run(report):
        raise AgentError(f"Critic rejected coder after revision: {report.summary}")
    return current


async def run_pipeline(redis: Redis, run_id: UUID, problem: ProblemInput) -> None:
    """Run the full 4-agent pipeline. Emit terminal `done` with paths + status."""
    settings = get_settings()
    runs_dir = Path(settings.runs_dir).resolve()  # noqa: ASYNC240 — stdlib asyncio, not trio
    run_dir = runs_dir / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    # Persist forensic events to disk; Redis stream MAXLEN=5000 rolls off the
    # rest. events.jsonl lets us reconstruct the full timeline post-failure.
    emitter = EventEmitter(redis, run_id, events_log_path=run_dir / "events.jsonl")

    gateway = GatewayClient(settings.gateway_http, settings.dev_auth_token)
    kernel = KernelSession(run_id, runs_dir)
    # MatlabSession is created upfront so backend detection runs once per run.
    # Detection is cheap (shutil.which); the backend may still be NoOp if
    # neither matlab nor octave is installed — that's surfaced as an error
    # cell when the Coder picks language='matlab' rather than crashing the run.
    matlab_session = MatlabSession(run_id, runs_dir)
    hmml = _get_hmml()

    try:
        try:
            # Run-level reasoning effort threaded into every agent so each
            # `gateway.stream_completion` call carries the hint verbatim. Per-
            # agent PromptSpec overrides win on a call-by-call basis.
            kwargs: dict[str, Any] = {
                "run_effort": problem.reasoning_effort,
                "long_context": problem.long_context,
                "model_override": problem.model_override,
            }

            analyzer = AnalyzerAgent(gateway, emitter, **kwargs)
            critic = CriticAgent(gateway, emitter, **kwargs)
            analysis = await analyzer.run_for_problem(problem)
            analysis = await _review_and_maybe_revise(
                critic=critic,
                producer=analyzer,
                target_agent="analyzer",
                output=analysis,
                context={
                    "problem_text": problem.problem_text,
                    "competition_type": problem.competition_type,
                },
                criteria=[
                    "Restates every sub-question in the problem.",
                    "Lists assumptions needed downstream.",
                    "Lists concrete data requirements.",
                    "Proposes at least one usable modeling approach.",
                ],
            )
            assert isinstance(analysis, AnalyzerOutput)

            searcher = SearcherAgent(gateway, emitter, runs_dir=runs_dir, **kwargs)
            findings = await searcher.run_for(problem, analysis)
            findings = await _review_and_maybe_revise(
                critic=critic,
                producer=searcher,
                target_agent="searcher",
                output=findings,
                context={
                    "problem_text": problem.problem_text,
                    "competition_type": problem.competition_type,
                    "analysis": analysis.model_dump(mode="json"),
                },
                criteria=_searcher_review_criteria(),
            )
            assert isinstance(findings, SearchFindings)

            modeler = ModelerAgent(gateway, emitter, hmml=hmml, **kwargs)
            spec = await modeler.run_for(problem, analysis)
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

            coder = CoderAgent(
                gateway, emitter, kernel, matlab_session=matlab_session, **kwargs
            )
            coder_out = await coder.run(problem, analysis, spec)
            coder_out = await _review_and_maybe_rerun_coder(
                critic=critic,
                coder=coder,
                problem=problem,
                analysis=analysis,
                spec=spec,
                coder_out=coder_out,
            )

            writer = WriterAgent(gateway, emitter, run_dir=run_dir, **kwargs)
            paper = await writer.run_for(
                problem, analysis, spec, coder_out, findings
            )

            # Deterministic evidence mining: surface concrete facts about the
            # Writer's own output before the LLM Critic sees it. Two scanners:
            # - sensitivity: ≥3 parameters perturbed at ±N% (F/O-prize bar)
            # - anonymity: school/region/author names (MCM/CUMCM instant DQ)
            # Findings turn into extra criteria so the Critic + revision loop
            # gets pointed at real flaws rather than hallucinated ones.
            base_writer_criteria = [
                "Abstract follows award-mode numeric-result rules.",
                "Every problem sub-question is answered explicitly.",
                "Sensitivity analysis and strengths/weaknesses are present when applicable.",
                "References are sufficient and cited in the body.",
                "Figures are referenced using known figure ids and discussed with numbers.",
                "No school, student, or identifying team information appears.",
            ]
            sens_findings = mine_sensitivity_evidence(paper)
            anon_findings = scan_anonymity_violations(paper)
            extra_criteria = sensitivity_criteria(sens_findings) + anonymity_criteria(
                anon_findings
            )
            if extra_criteria:
                await emitter.emit(
                    "log",
                    {
                        "level": "warning" if anon_findings.has_violations else "info",
                        "message": (
                            "deterministic Writer scan flagged "
                            f"{len(extra_criteria)} issue(s): "
                            f"sensitivity_params={sens_findings.parameter_count_estimate}, "
                            f"perturb_mentions={sens_findings.perturb_mention_count}, "
                            f"anonymity_hits={len(anon_findings.violations)}"
                        ),
                    },
                    agent="writer",
                )

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
                    "evidence_scan": {
                        "sensitivity": {
                            "has_section": sens_findings.has_sensitivity_section,
                            "parameter_count_estimate": sens_findings.parameter_count_estimate,
                            "perturb_mentions": sens_findings.perturb_mention_count,
                            "tornado_or_mc_referenced": sens_findings.tornado_or_mc_referenced,
                        },
                        "anonymity_violations": [
                            {"location": loc, "snippet": snip}
                            for loc, snip in anon_findings.violations
                        ],
                    },
                },
                criteria=base_writer_criteria + extra_criteria,
            )
            assert isinstance(paper, PaperDraft)

            # Resolve `[[FIG:<id>]]` placeholders in the Writer's output:
            # the on-disk paper.md gets real markdown image syntax (for the
            # preview UI), while paper.meta.json preserves placeholders for
            # the downstream LaTeX/DOCX/PDF exporter to render natively.
            paper_path = run_dir / "paper.md"
            paper_md = _render_paper_markdown(
                _substitute_figure_placeholders(paper, coder_out.figures, emitter_log=_log)
            )
            paper_path.write_text(paper_md, encoding="utf-8")  # noqa: ASYNC240

            meta_path = run_dir / "paper.meta.json"
            meta_path.write_text(
                json.dumps(
                    _build_paper_meta(problem, paper, coder_out.figures),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )  # noqa: ASYNC240

            # Auto-export PDF via gateway (tectonic + pandoc). Failure here
            # is non-fatal: paper.md stays the source of truth. The PDF path
            # is included in `done` only on success.
            pdf_path = await _auto_export_pdf(
                gateway=gateway,
                run_id=run_id,
                run_dir=run_dir,
                settings=settings,
                emitter=emitter,
            )

            # Do NOT include `cost_rmb` here: the gateway's cost.rs already
            # maintains runs.cost_rmb authoritatively from per-call cost events.
            # Setting cost_rmb=0 in the done payload would cause the audit task
            # to overwrite the correct accumulated total with zero.
            done_payload: dict[str, Any] = {
                "status": "success",
                "notebook_path": coder_out.notebook_path,
                "paper_path": str(paper_path),
                "meta_path": str(meta_path),
            }
            if pdf_path is not None:
                done_payload["pdf_path"] = str(pdf_path)
            await emitter.emit("done", done_payload, agent=None)
        except AgentError as exc:
            # Surface the real reason before declaring failure. Without this
            # the run terminates silently and forensics requires hand-walking
            # the Redis stream and DB.
            _log.exception("pipeline failed: %s", exc)
            await emitter.emit(
                "error",
                {
                    "message": str(exc),
                    "code": "agent_error",
                    "stage": None,
                },
                agent=None,
            )
            await emitter.emit("done", {"status": "failed"}, agent=None)
    finally:
        await gateway.close()


async def _auto_export_pdf(
    *,
    gateway: GatewayClient,
    run_id: UUID,
    run_dir: Path,
    settings: Any,
    emitter: EventEmitter,
) -> Path | None:
    """Trigger the gateway export endpoint and persist paper.pdf in run_dir.

    Returns the absolute Path on success, None on any failure. Always
    non-fatal: emits a `log` event with the reason so users can diagnose.
    Disabled via `MM_AUTO_EXPORT_PDF=0`.
    """
    if not getattr(settings, "auto_export_pdf", True):
        return None
    import httpx

    pdf_path = run_dir / "paper.pdf"
    try:
        await emitter.emit(
            "log",
            {
                "level": "info",
                "message": "auto-exporting paper.pdf via gateway/export/pdf",
            },
            agent=None,
        )
        pdf_bytes = await gateway.export_paper(
            run_id=run_id,
            format="pdf",
            compile_timeout_s=getattr(settings, "auto_export_timeout_s", 600.0),
        )
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            await emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": (
                        "auto-export returned non-PDF payload "
                        f"({len(pdf_bytes)} bytes); paper.md remains canonical"
                    ),
                },
                agent=None,
            )
            return None
        pdf_path.write_bytes(pdf_bytes)  # noqa: ASYNC240
        await emitter.emit(
            "log",
            {
                "level": "info",
                "message": f"paper.pdf written ({len(pdf_bytes)} bytes)",
            },
            agent=None,
        )
        return pdf_path
    except (httpx.HTTPError, OSError) as exc:  # noqa: BLE001 — narrow set
        _log.warning("auto-export PDF failed: %s", exc)
        await emitter.emit(
            "log",
            {
                "level": "warning",
                "message": f"auto-export PDF failed: {exc}; paper.md remains canonical",
            },
            agent=None,
        )
        return None


_FIG_PLACEHOLDER_RE = re.compile(r"\[\[FIG:([a-z0-9_]+)\]\]")


def _substitute_figure_placeholders(
    paper: PaperDraft,
    figures: list[Figure],
    emitter_log: logging.Logger = _log,
) -> PaperDraft:
    """Return a copy of `paper` with `[[FIG:<id>]]` replaced by markdown images.

    Unknown ids are dropped with a warning so a single hallucinated reference
    doesn't leave broken markup in the preview. The figure list is the source
    of truth — the Coder shipped it, the Writer may only reference it.
    """
    by_id: dict[str, Figure] = {f.id: f for f in figures}

    def _repl(match: re.Match[str]) -> str:
        fig_id = match.group(1)
        fig = by_id.get(fig_id)
        if fig is None:
            emitter_log.warning(
                "Writer referenced unknown figure id %r; placeholder dropped",
                fig_id,
            )
            return ""
        # Blank lines around the image so the markdown renderer always treats
        # it as a block, regardless of the surrounding paragraph.
        # Markdown rendering uses native `![]()` syntax. We deliberately do
        # NOT emit a second `*图: ...*` line — that bilingual prefix leaked
        # into the rendered PDF as raw text (judges noticed). The image
        # `alt` text + the Writer's own surrounding prose carry the caption.
        return f"\n\n![{fig.caption}]({fig.path_png})\n\n"

    new_sections = [
        type(s)(
            title=s.title,
            body_markdown=_FIG_PLACEHOLDER_RE.sub(_repl, s.body_markdown),
        )
        for s in paper.sections
    ]
    return PaperDraft(
        title=paper.title,
        abstract=paper.abstract,
        sections=new_sections,
        references=paper.references,
        figure_refs=paper.figure_refs,
    )


def _build_paper_meta(
    problem: ProblemInput,
    paper: PaperDraft,
    figures: list[Figure],
) -> dict[str, Any]:
    """Structured export payload consumed by the gateway's PDF/DOCX/LaTeX path.

    Sections keep the raw `[[FIG:<id>]]` placeholders so the exporter can
    render each figure natively (e.g. `\\includegraphics[width=0.8\\textwidth]`
    for LaTeX) instead of parsing markdown image syntax.
    """
    return {
        "title": paper.title,
        "abstract": paper.abstract,
        "competition_type": problem.competition_type,
        "problem_text": problem.problem_text,
        "sections": [
            {"title": s.title, "body_markdown": s.body_markdown}
            for s in paper.sections
        ],
        "references": list(paper.references),
        "figures": [f.model_dump(mode="json") for f in figures],
    }


def _render_paper_markdown(paper: PaperDraft) -> str:
    """Render a PaperDraft to a Markdown document string."""
    parts: list[str] = [f"# {paper.title}", "", "## Abstract", "", paper.abstract]
    for section in paper.sections:
        parts.extend(["", f"## {section.title}", "", section.body_markdown])
    if paper.references:
        parts.extend(["", "## References", ""])
        for i, ref in enumerate(paper.references, start=1):
            parts.append(f"{i}. {ref}")
    # Ensure trailing newline for POSIX-friendly files.
    return "\n".join(parts) + "\n"


__all__ = ["run_pipeline"]
