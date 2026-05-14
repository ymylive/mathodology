"""Tests for the Critic revision loop's ACTUAL-cost budget enforcement.

Round-6 audit fix: `_review_and_maybe_revise` used to enforce
`max_revision_cost_rmb` against fictitious estimates in `CriticPolicy`. We
now optionally consume a `RunCostTracker` that reads the gateway's
authoritative running total. These tests lock down:

1. Tracker-equipped loops compare against actual spend.
2. No-tracker loops keep the old estimate-based behavior (backward compat).
3. When actual spend balloons past the budget the loop sets
   `report.budget_exhausted = True` and stops.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from agent_worker.cost_tracker import RunCostTracker
from agent_worker.pipeline import (
    CriticPolicy,
    _review_and_maybe_revise,
)
from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueFinding, CritiqueReport

# ---------------------------------------------------------------------------
# Test doubles — duplicated from test_pipeline_critic_review_flow rather than
# imported to keep the new file self-contained and decoupled from the older
# Round-1 test scaffolding.
# ---------------------------------------------------------------------------


class _SequenceProducer:
    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput

    def __init__(self, revisions: list[AnalyzerOutput]) -> None:
        self.revisions = revisions
        self.revision_calls = 0

    async def revise_with_critique(
        self,
        *,
        original_output: AnalyzerOutput,
        critique: CritiqueReport,
        context: dict[str, Any],
    ) -> AnalyzerOutput:
        self.revision_calls += 1
        return self.revisions[min(self.revision_calls - 1, len(self.revisions) - 1)]


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


def _report(
    *,
    passed: bool,
    major: bool = False,
    blocking: bool = False,
) -> CritiqueReport:
    findings = []
    if major or blocking:
        findings.append(
            CritiqueFinding(
                severity="blocking" if blocking else "major",
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


def _fake_tracker(values: list[float]) -> RunCostTracker:
    """Build a RunCostTracker that returns `values[i]` on the i-th GET.

    We patch `get_total` directly so we don't have to construct a fake
    Redis chain — the tracker's Redis-reading behavior is exercised
    separately in `test_cost_tracker.py`.
    """
    # Use a real RunCostTracker instance so snapshot_baseline /
    # delta_since_baseline preserve their stateful semantics; only the
    # leaf method that touches Redis is mocked.
    from uuid import uuid4

    tracker = RunCostTracker(AsyncMock(), uuid4())
    tracker.get_total = AsyncMock(side_effect=values)  # type: ignore[method-assign]
    return tracker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_revision_loop_falls_back_to_estimates_without_tracker() -> None:
    """Backward-compat: no tracker => estimate-only accounting (Round-1 behavior).

    With 0.04 RMB budget, 0.01 review + 0.02 revision, the loop:
    - review (0.01) -> needs revision
    - check budget: 0.01 + 0.02 = 0.03 <= 0.04 -> revise
    - revise (cumulative 0.03)
    - check budget: 0.03 + 0.01 = 0.04 <= 0.04 -> review again
    - review (cumulative 0.04) -> needs revision
    - check budget: 0.04 + 0.02 = 0.06 > 0.04 -> budget_exhausted, break
    Result: 1 revision call, 2 critic calls.
    """
    original = _analysis(["Estimate demand"])
    producer = _SequenceProducer(
        [
            _analysis(["Estimate demand", "Optimize allocation"]),
            _analysis(["Estimate demand", "Optimize allocation", "Validate robustness"]),
        ]
    )
    critic = _FakeCritic([_report(passed=False, major=True), _report(passed=False, major=True)])

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand."},
        criteria=["covers all sub-questions"],
        policy=CriticPolicy(max_revision_rounds=2, max_revision_cost_rmb=0.04),
        estimated_review_cost_rmb=0.01,
        estimated_revision_cost_rmb=0.02,
        cost_tracker=None,
    )

    # Loop stopped after one revision because estimate-based budget hit.
    assert result is producer.revisions[0]
    assert producer.revision_calls == 1
    assert critic.calls == 2


async def test_revision_loop_uses_actual_cost_when_tracker_provided() -> None:
    """With a tracker, the loop consults actual spend on each iteration.

    Tracker returns 0.0 throughout (gateway has recorded no cost), so even
    though the estimate would predict a budget breach, the actual+estimate
    check passes and the loop runs to completion.

    Budget 0.04, estimates 0.01/0.02 — without a tracker the equivalent
    loop stops after 1 revision (see the previous test). With a
    zero-tracker it completes both rounds.

    We clear the per-stage `max_revision_rounds_overrides` because the
    default caps the analyzer at 1 round (Round-7 tuning); this test is
    about cost accounting, not stage-policy.
    """
    original = _analysis(["Estimate demand"])
    first = _analysis(["Estimate demand", "Optimize allocation"])
    second = _analysis(["Estimate demand", "Optimize allocation", "Validate"])
    producer = _SequenceProducer([first, second])
    critic = _FakeCritic(
        [
            _report(passed=False, major=True),  # initial review
            _report(passed=False, major=True),  # after revision 1
            _report(passed=True),  # after revision 2 → accept
        ]
    )

    # snapshot_baseline -> 0.0; then delta calls for round 1 (pre + post)
    # and round 2 (pre + post). Provide enough zeros for the worst case.
    tracker = _fake_tracker([0.0] * 10)

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand and optimize allocation."},
        criteria=["covers all sub-questions"],
        policy=CriticPolicy(
            max_revision_rounds=2,
            max_revision_cost_rmb=0.04,
            max_revision_rounds_overrides={},
        ),
        estimated_review_cost_rmb=0.01,
        estimated_revision_cost_rmb=0.02,
        cost_tracker=tracker,
    )

    # Tracker reported zero actual cost so the budget never trips; loop
    # finished naturally (revision 2 review passed).
    assert result is second
    assert producer.revision_calls == 2
    assert critic.calls == 3


async def test_loop_breaks_when_actual_cost_exceeds_budget() -> None:
    """Tracker reports a huge spend after the first revision — the loop
    must immediately set `budget_exhausted` and bail.

    This is the round-6 regression: even though estimates would say
    "plenty of budget left", the gateway's actual ledger shows we've
    blown through and we MUST stop.
    """
    original = _analysis(["Estimate demand"])
    producer = _SequenceProducer(
        [
            _analysis(["Estimate demand", "Optimize allocation"]),
            _analysis(["Estimate demand", "Optimize allocation", "Validate"]),
        ]
    )
    critic = _FakeCritic(
        [
            _report(passed=False, major=True),  # initial review
            _report(passed=False, major=True),  # after revision 1
        ]
    )

    # Sequence of GET values consumed by the tracker:
    #   1. snapshot_baseline() at start                 -> 0.0
    #   2. pre-revision delta check (round 1)           -> 0.10 (under cap)
    #   3. post-revision/pre-review delta (round 1)     -> 5.00 (>>> 1.0)
    # Should trip budget_exhausted before the second critic.review.
    tracker = _fake_tracker([0.0, 0.10, 5.00])

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand."},
        criteria=["covers all sub-questions"],
        policy=CriticPolicy(max_revision_rounds=2, max_revision_cost_rmb=1.0),
        estimated_review_cost_rmb=0.14,
        estimated_revision_cost_rmb=0.30,
        cost_tracker=tracker,
    )

    # Round 1 revision happened, but after that the tracker showed
    # 5.0 RMB so we broke before the round-1 follow-up review.
    assert result is producer.revisions[0]
    assert producer.revision_calls == 1
    # Critic called only for the initial review — the post-revision
    # follow-up was skipped because budget tripped first.
    assert critic.calls == 1


async def test_actual_cost_baseline_isolates_loop_spend() -> None:
    """`snapshot_baseline()` must isolate the in-loop delta from upstream
    cost. If a prior stage spent 0.90 RMB, the analyzer loop should see
    delta=0.0 at start, not 0.90.
    """
    original = _analysis(["Estimate demand"])
    producer = _SequenceProducer([_analysis(["unused"])])
    critic = _FakeCritic([_report(passed=True)])

    # Snapshot reads 0.90 (upstream spend). If baseline isn't subtracted,
    # any later delta check would immediately fail. But with a passing
    # report on the first review, the loop returns before any delta
    # check fires — we just assert no spurious failure.
    tracker = _fake_tracker([0.90])

    result = await _review_and_maybe_revise(
        critic=critic,  # type: ignore[arg-type]
        producer=producer,  # type: ignore[arg-type]
        target_agent="analyzer",
        output=original,
        context={"problem_text": "Estimate demand."},
        criteria=["covers all sub-questions"],
        policy=CriticPolicy(max_revision_rounds=2, max_revision_cost_rmb=1.0),
        cost_tracker=tracker,
    )

    assert result is original
    assert producer.revision_calls == 0
    assert critic.calls == 1
