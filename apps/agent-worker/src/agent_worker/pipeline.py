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
import time
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
from agent_worker.audit import run_paper_audit
from agent_worker.cancellation import CancellationChecker, RunCancelled
from agent_worker.config import get_settings
from agent_worker.cost_tracker import RunCostTracker
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.hmml import HMMLService
from agent_worker.kernel import KernelSession
from agent_worker.matlab import MatlabSession
from agent_worker.skills import SkillRegistry, load_skills_dir

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticPolicy:
    min_score: float = 0.80
    min_checklist_pass_rate: float = 0.85
    max_revision_rounds: int = 2  # default fallback
    coder_revision_iterations: int = 2
    max_revision_cost_rmb: float = 1.00
    # Round-6 cost audit corrected these from 0.02/0.05/0.12 — they were a 7×
    # underestimate so revisions ran well past the max_revision_cost_rmb cap.
    # The cap behavior is unchanged; only the per-iteration accounting is now
    # accurate, so the loop actually halts at ~1.00 RMB.
    estimated_review_cost_rmb: float = 0.14
    estimated_revision_cost_rmb: float = 0.30
    estimated_coder_revision_cost_rmb: float = 0.18
    min_score_overrides: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({"searcher": 0.75})
    )
    min_checklist_pass_rate_overrides: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({"searcher": 0.80})
    )
    # Per-stage cap on revision rounds. Writer:1 because round-3 only fixes
    # ~3% of issues but costs 0.62 RMB (round-6 cost audit). Analyzer and
    # Searcher also capped at 1 — they're cheap but marginal returns drop
    # off quickly. Modeler and Coder stay at the default (2).
    max_revision_rounds_overrides: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType(
            {"writer": 1, "analyzer": 1, "searcher": 1}
        )
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


def _load_pipeline_skills() -> SkillRegistry:
    """Load the SKILL.md files from ``docs/skills`` for on-demand body lookup.

    Resolution walks up from this file to find ``docs/skills`` so the
    function works both from the installed package and the source tree.
    Missing dir is non-fatal — registry is just empty and the Coder skips
    the get_skill tool entirely.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "docs" / "skills"
        if candidate.exists():
            try:
                return load_skills_dir(candidate)
            except Exception as e:  # noqa: BLE001 — skill discovery must never fail a run
                _log.warning("skill discovery failed at %s: %s", candidate, e)
                return SkillRegistry([])
    _log.debug("no docs/skills directory found; skill tool will be inert")
    return SkillRegistry([])


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


async def _run_audit_and_maybe_revise(
    *,
    paper: BaseModel,  # PaperDraft, but typed loose to avoid forward import
    paper_md_for_check: str,
    coder_out: BaseModel,  # CoderOutput
    analysis: BaseModel,  # AnalyzerOutput
    run_dir: Path,
    writer: BaseAgent,
    critic: CriticAgent,
    writer_criteria: list[str],
    writer_context: dict[str, Any],
    cost_tracker: RunCostTracker | None,
    emitter: EventEmitter,
    cancel: CancellationChecker,
) -> BaseModel:
    """Pre-submission audit gate.

    Runs the rule-based checks in `audit.py`. If any blocking findings
    are targeted at `writer`, drive ONE revision pass through the
    existing `revise_with_critique` flow with an injected audit hint
    so the Writer sees the concrete issues to fix. Findings targeted
    at `coder` (e.g. broken CJK fonts in figures) are too expensive to
    auto-fix here — surface them as `log.warning` events for the user
    to see in the UI.

    Returns the (possibly revised) paper.
    """
    from mm_contracts import (  # local import to dodge cycle pressure
        AnalyzerOutput,
        CoderOutput,
        CritiqueFinding,
        CritiqueReport,
        PaperDraft,
    )

    assert isinstance(paper, PaperDraft)
    assert isinstance(coder_out, CoderOutput)
    assert isinstance(analysis, AnalyzerOutput)

    await emitter.emit(
        "stage.start", {"stage": "audit"}, agent="audit"
    )
    t0_audit = time.monotonic()
    report = run_paper_audit(
        paper=paper,
        paper_md=paper_md_for_check,
        coder_out=coder_out,
        analysis=analysis,
        run_dir=run_dir,
    )

    # Always surface findings — even non-blocking ones — so the user can
    # see the gate is doing something.
    for f in report.findings:
        await emitter.emit(
            "log",
            {
                "level": "warning" if f.severity == "blocking" else "info",
                "message": f"audit/{f.code}: {f.message}",
            },
            agent="audit",
        )

    audit_dur_ms = int((time.monotonic() - t0_audit) * 1000)
    await emitter.emit(
        "stage.done",
        {
            "stage": "audit",
            "duration_ms": audit_dur_ms,
            "passed": report.passed,
            "finding_count": len(report.findings),
        },
        agent="audit",
    )

    if report.passed:
        return paper

    writer_blocking = report.blocking_for("writer")
    if not writer_blocking:
        # Only Coder-side findings remain — log and ship (re-running
        # Coder here would double the run cost; better to let the user
        # decide via fine-tune).
        return paper

    # One revision pass driven by the audit hint. Reuse the existing
    # `revise_with_critique` contract by synthesising a CritiqueReport
    # whose `summary` carries the merged audit hint — the Writer's
    # prompt already knows how to consume that shape.
    audit_hint = report.merged_hint_for("writer")
    fake_critique = CritiqueReport(
        target_agent="writer",
        target_schema="PaperDraft",
        scores={"audit": 0.0},
        passed=False,
        has_blocking_findings=True,
        major_findings=[
            CritiqueFinding(
                dimension="audit",
                severity="major",
                description=f.message,
                fix_hint=f.fix_hint,
            )
            for f in writer_blocking
        ],
        minor_findings=[],
        summary=audit_hint,
        checklist_pass_rate=0.0,
        recommended_action="revise",
    )

    await cancel.check_or_raise()
    await emitter.emit(
        "log",
        {
            "level": "info",
            "message": (
                f"audit bouncing back to writer for {len(writer_blocking)} "
                "blocking finding(s)"
            ),
        },
        agent="audit",
    )
    try:
        revised = await writer.revise_with_critique(
            original_output=paper,
            critique=fake_critique,
            context=writer_context,
        )
    except AgentError as exc:
        # Writer revision crashed — log and ship the original. Better
        # than a hard failure when we already have a paper in hand.
        _log.warning("audit-triggered writer revision failed: %s", exc)
        await emitter.emit(
            "log",
            {
                "level": "warning",
                "message": (
                    "audit revision attempt failed; shipping original paper: "
                    f"{exc}"
                ),
            },
            agent="audit",
        )
        return paper
    if cost_tracker is not None:
        # Refresh the run's spend so downstream stages (if any) see the
        # post-audit number.
        await cost_tracker.get_total()
    assert isinstance(revised, PaperDraft)

    # Re-run audit on the revised paper to confirm we're shipping
    # something cleaner — this is best-effort signal, not a second
    # revision loop.
    revised_md = _render_paper_markdown(
        _substitute_figure_placeholders(revised, coder_out.figures, emitter_log=_log)
    )
    second_report = run_paper_audit(
        paper=revised,
        paper_md=revised_md,
        coder_out=coder_out,
        analysis=analysis,
        run_dir=run_dir,
    )
    delta = len(report.findings) - len(second_report.findings)
    await emitter.emit(
        "log",
        {
            "level": "info" if delta >= 0 else "warning",
            "message": (
                f"audit second pass: {len(second_report.findings)} finding(s) "
                f"remaining (was {len(report.findings)})"
            ),
        },
        agent="audit",
    )
    return revised


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
    cost_tracker: RunCostTracker | None = None,
    reminders_out: dict[str, str] | None = None,
) -> BaseModel:
    """Critic-driven review with up to `max_revision_rounds` retries.

    When `reminders_out` is provided, the FINAL accepted verdict's
    `additional_context` (computed by `critique_to_hook_result`) is
    written into it under the *next* stage's name. Downstream agents
    then render this string as a `<system_reminder>` block in their
    user prompt — that's how Critic minor-finding feedback flows
    forward without a full revision round.

    When `cost_tracker` is provided, the revision-cost budget is enforced
    against the gateway's ACTUAL cost-ledger total (read from
    `mm:cost:<run_id>`) rather than the estimates in
    `CriticPolicy.estimated_*_cost_rmb`. The estimate is still added on
    top to predict the NEXT call so we stop *before* exceeding the
    budget, not after. Without a tracker the function falls back to
    estimate-only accounting (backward-compatible default).
    """
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
    # Round-7: writer is capped at 1 revision (3% issue-fix rate vs 0.62 RMB
    # per round). Other agents fall through to the default (2).
    max_rounds = policy.max_revision_rounds_overrides.get(
        target_agent, policy.max_revision_rounds
    )

    async def _spent_so_far(estimate: float) -> float:
        """Return authoritative spend (tracker delta) or estimate fallback.

        The tracker reads `mm:cost:<run_id>` which the gateway
        INCRBYFLOATs on every chargeable LLM call. Falling back to
        `estimate` when no tracker is provided keeps the unit tests
        (and any caller that hasn't wired Redis through yet) working.
        """
        if cost_tracker is None:
            return estimate
        return await cost_tracker.delta_since_baseline()

    if cost_tracker is not None:
        # Anchor the budget at "what's been spent inside this loop only".
        # Without a baseline the delta would include all upstream stage
        # cost, so we'd start `budget_exhausted=True` immediately on any
        # non-trivial run.
        await cost_tracker.snapshot_baseline()

    current = output
    loop_cost_rmb = 0.0
    report = await critic.review(
        target_agent=target_agent,  # type: ignore[arg-type]
        target_schema=type(current).__name__,
        artifact=current.model_dump(mode="json"),
        context=context,
        criteria=criteria,
        revision_round=0,
        max_revision_rounds=max_rounds,
    )
    loop_cost_rmb += review_cost_rmb
    if not _critique_requires_revision(report, policy):
        return current

    for revision_round in range(1, max_rounds + 1):
        actual = await _spent_so_far(loop_cost_rmb)
        if actual + revision_cost_rmb > policy.max_revision_cost_rmb:
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
        actual = await _spent_so_far(loop_cost_rmb)
        if actual + review_cost_rmb > policy.max_revision_cost_rmb:
            report.budget_exhausted = True
            break
        report = await critic.review(
            target_agent=target_agent,  # type: ignore[arg-type]
            target_schema=type(current).__name__,
            artifact=current.model_dump(mode="json"),
            context=context,
            criteria=criteria,
            revision_round=revision_round,
            max_revision_rounds=max_rounds,
        )
        loop_cost_rmb += review_cost_rmb
        if not _critique_requires_revision(report, policy):
            return current

    if _critique_should_fail_run(report):
        # Round-10 QA: a Critic verdict like "mostly appropriate ... but
        # not ready" used to nuke the whole run (¥4+ wasted). User would
        # rather see the imperfect artifact than nothing. Emit a loud
        # warning and proceed — the next stage's prompt picks the reminder
        # up via `upstream_reminders` so quality concerns still propagate.
        await critic.emitter.emit(
            "log",
            {
                "level": "warning",
                "message": (
                    f"Critic flagged {target_agent} after revision exhausted: "
                    f"{report.summary[:240]}"
                ),
            },
            agent=target_agent,
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
    next_stage = _next_stage_after(target_agent)
    if reminder:
        if reminders_out is not None and next_stage is not None:
            reminders_out[next_stage] = reminder

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
                    "for_stage": next_stage,
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
    cost_tracker: RunCostTracker | None = None,
    reminders_out: dict[str, str] | None = None,
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
    # Round-7: coder falls through to the default (2) because it's not in
    # the overrides map — coder reruns actually fix bugs at a high rate.
    max_rounds = policy.max_revision_rounds_overrides.get(
        "coder", policy.max_revision_rounds
    )

    async def _spent_so_far(estimate: float) -> float:
        if cost_tracker is None:
            return estimate
        return await cost_tracker.delta_since_baseline()

    if cost_tracker is not None:
        await cost_tracker.snapshot_baseline()

    report = await critic.review(
        target_agent="coder",
        target_schema="CoderOutput",
        artifact=coder_out.model_dump(mode="json"),
        context=context,
        criteria=criteria,
        revision_round=0,
        max_revision_rounds=max_rounds,
    )
    loop_cost_rmb = policy.estimated_review_cost_rmb
    if not _critique_requires_revision(report, policy):
        return coder_out

    current = coder_out
    for revision_round in range(1, max_rounds + 1):
        actual = await _spent_so_far(loop_cost_rmb)
        if actual + policy.estimated_coder_revision_cost_rmb > policy.max_revision_cost_rmb:
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
        actual = await _spent_so_far(loop_cost_rmb)
        if actual + policy.estimated_review_cost_rmb > policy.max_revision_cost_rmb:
            report.budget_exhausted = True
            break
        report = await critic.review(
            target_agent="coder",
            target_schema="CoderOutput",
            artifact=current.model_dump(mode="json"),
            context=context,
            criteria=criteria,
            revision_round=revision_round,
            max_revision_rounds=max_rounds,
        )
        loop_cost_rmb += policy.estimated_review_cost_rmb
        if not _critique_requires_revision(report, policy):
            return current

    if _critique_should_fail_run(report):
        # Same relaxation as _review_and_maybe_revise: ship the imperfect
        # artifact with a loud warning instead of nuking the run. Coder's
        # "post-revision rejection" is almost always a quality concern, not
        # a structural failure — the notebook ran, cells executed, figures
        # were produced. User gets a paper they can fine-tune via the
        # FinetuneChat panel; a hard failure is harsher than warranted.
        await critic.emitter.emit(
            "log",
            {
                "level": "warning",
                "message": (
                    "Critic flagged coder after revision exhausted: "
                    f"{report.summary[:240]}"
                ),
            },
            agent="coder",
        )

    # Same post-stage hook lifecycle as _review_and_maybe_revise — surface
    # a `<system_reminder>` for the next stage (writer) when the final
    # verdict has minor findings worth carrying forward.
    hook_result = critique_to_hook_result(report)
    agg = aggregate([hook_result])
    reminder = agg.merged_reminder()
    if reminder and reminders_out is not None:
        reminders_out["writer"] = reminder
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
    # Skill registry — round-8 on-demand body loading. CoderAgent renders
    # the frontmatter-only menu into its system prompt and looks up bodies
    # via `get_skill` only when it decides one is relevant, instead of
    # eagerly inlining all 8+ kernel/MATLAB skill bodies into every turn.
    # Empty registry (no docs/skills/* on disk) is a clean no-op — the
    # tool simply isn't exposed to the LLM.
    skill_registry: SkillRegistry = _load_pipeline_skills()

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

            # Critic builds its own kwargs because the run-level model_override
            # (e.g. gpt-5.5 for the producer agents) is usually wrong for
            # Critic — Critic emits a small JSON verdict, not prose, so a
            # cheaper model handles it well. MM_CRITIC_MODEL_OVERRIDE wins
            # over the run-level pick when set. Empty = falls through.
            critic_kwargs: dict[str, Any] = dict(kwargs)
            if settings.critic_model_override:
                critic_kwargs["model_override"] = settings.critic_model_override

            # Shared dict for post-stage hook reminders. Keys are stage
            # names ("searcher", "modeler", "coder", "writer"). Each entry
            # is a `<system_reminder>` XML block produced by
            # `critique_to_hook_result()` after the upstream Critic verdict.
            # Downstream agents render it into their user_template.
            upstream_reminders: dict[str, str] = {}

            analyzer = AnalyzerAgent(gateway, emitter, **kwargs)
            critic = CriticAgent(gateway, emitter, **critic_kwargs)

            # Round-6 follow-up: read the gateway's authoritative cost
            # ledger (Redis key `mm:cost:<run_id>`, INCRBYFLOAT'd by
            # `gateway::llm::cost::record_completion_cost`) so the Critic
            # revision budget compares actual spend, not estimates.
            cost_tracker = RunCostTracker(redis, run_id)

            # Round-7: mid-run cancellation. The gateway writes
            # `mm:cancel:<run_id> = "1"` on `POST /runs/:id/cancel`; the
            # worker polls this key at every stage boundary (and at each
            # Coder turn) so users can halt a 90-min pipeline without
            # waiting it out. `RunCancelled` is an `AgentError` subclass
            # — caught by the outer try below and converted to a clean
            # `done(status="cancelled")` event with partial artifacts.
            cancel = CancellationChecker(redis, run_id)

            await cancel.check_or_raise()
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
                cost_tracker=cost_tracker,
                reminders_out=upstream_reminders,
            )
            assert isinstance(analysis, AnalyzerOutput)

            await cancel.check_or_raise()
            searcher = SearcherAgent(gateway, emitter, runs_dir=runs_dir, **kwargs)
            findings = await searcher.run_for(
                problem,
                analysis,
                upstream_reminders=upstream_reminders.get("searcher", ""),
            )
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
                cost_tracker=cost_tracker,
                reminders_out=upstream_reminders,
            )
            assert isinstance(findings, SearchFindings)

            await cancel.check_or_raise()
            modeler = ModelerAgent(gateway, emitter, hmml=hmml, **kwargs)
            spec = await modeler.run_for(
                problem,
                analysis,
                upstream_reminders=upstream_reminders.get("modeler", ""),
            )
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
                cost_tracker=cost_tracker,
                reminders_out=upstream_reminders,
            )
            assert isinstance(spec, ModelSpec)

            await cancel.check_or_raise()
            coder = CoderAgent(
                gateway,
                emitter,
                kernel,
                matlab_session=matlab_session,
                skill_registry=skill_registry,
                **kwargs,
            )
            coder_out = await coder.run(
                problem,
                analysis,
                spec,
                upstream_reminders=upstream_reminders.get("coder", ""),
            )
            coder_out = await _review_and_maybe_rerun_coder(
                critic=critic,
                coder=coder,
                problem=problem,
                analysis=analysis,
                spec=spec,
                coder_out=coder_out,
                cost_tracker=cost_tracker,
                reminders_out=upstream_reminders,
            )

            await cancel.check_or_raise()
            writer = WriterAgent(gateway, emitter, run_dir=run_dir, **kwargs)
            paper = await writer.run_for(
                problem,
                analysis,
                spec,
                coder_out,
                findings,
                upstream_reminders=upstream_reminders.get("writer", ""),
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
                cost_tracker=cost_tracker,
                reminders_out=upstream_reminders,  # final stage; for post-mortem
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

            # Pre-submission audit gate (round-10 follow-up). Runs after
            # Writer + its Critic pass have settled. Catches issues no
            # per-stage Critic can: orphan figures, sparse references,
            # uncovered sub-questions, CJK-broken figures. Findings
            # tagged `dispatch_to="writer"` trigger ONE revision pass
            # against the original Writer with the audit hint injected
            # as a system reminder; Coder-targeted findings surface as
            # warnings (re-running Coder is too expensive for v1).
            paper = await _run_audit_and_maybe_revise(
                paper=paper,
                paper_md_for_check=_render_paper_markdown(
                    _substitute_figure_placeholders(
                        paper, coder_out.figures, emitter_log=_log
                    )
                ),
                coder_out=coder_out,
                analysis=analysis,
                run_dir=run_dir,
                writer=writer,
                critic=critic,
                writer_criteria=base_writer_criteria + extra_criteria,
                writer_context={
                    "problem_text": problem.problem_text,
                    "competition_type": problem.competition_type,
                    "analysis": analysis.model_dump(mode="json"),
                    "spec": spec.model_dump(mode="json"),
                    "coder_output": coder_out.model_dump(mode="json"),
                    "search_findings": findings.model_dump(mode="json"),
                },
                cost_tracker=cost_tracker,
                emitter=emitter,
                cancel=cancel,
            )

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
        except RunCancelled as exc:
            # User-initiated cancel. Differentiate from "failed" so the UI
            # shows it as an intentional stop (status='cancelled'), and so
            # the cost ledger isn't treated as a bug — we want this in the
            # dashboard. Whatever partial artifacts exist (paper.md from a
            # prior stage, notebook from Coder) are left on disk; the user
            # can inspect via the existing serve_paper / serve_notebook
            # routes.
            _log.info("pipeline cancelled by user: %s", exc)
            await emitter.emit(
                "log",
                {"level": "info", "message": "run cancelled by user"},
                agent=None,
            )
            await emitter.emit("done", {"status": "cancelled"}, agent=None)
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


_REF_PREFIX_RE = re.compile(r"^\s*\[(\d+)\]\s*")


def _render_paper_markdown(paper: PaperDraft) -> str:
    """Render a PaperDraft to a Markdown document string."""
    parts: list[str] = [f"# {paper.title}", "", "## Abstract", "", paper.abstract]
    for section in paper.sections:
        parts.extend(["", f"## {section.title}", "", section.body_markdown])
    if paper.references:
        parts.extend(["", "## References", ""])
        # The Writer naturally emits references with a `[N]` prefix so they
        # match the inline citations in the body (`...has been studied[1]`).
        # Using a markdown ordered list here would double-number them as
        # `1. [1] Arunraj...`. Render each reference on its own line as a
        # paragraph; markdown then renders them as the bibliography. If the
        # Writer DIDN'T prefix with `[N]`, fall through to auto-numbering.
        has_bracket_numbers = all(
            _REF_PREFIX_RE.match(r) for r in paper.references if r.strip()
        )
        for i, ref in enumerate(paper.references, start=1):
            if has_bracket_numbers:
                parts.append(ref)
                parts.append("")
            else:
                parts.append(f"{i}. {ref}")
    # Ensure trailing newline for POSIX-friendly files.
    return "\n".join(parts) + "\n"


__all__ = ["run_pipeline"]
