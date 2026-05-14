"""Round-7 cost-cap: per-stage max_revision_rounds overrides.

Writer/Analyzer/Searcher are capped at 1 revision round (round-6 cost audit
found round-3 only fixed ~3% of Writer issues at 0.62 RMB/round). Modeler
and Coder fall through to the default (2) because their reruns reliably
fix bugs.
"""

from __future__ import annotations

from types import MappingProxyType

from agent_worker.pipeline import DEFAULT_CRITIC_POLICY, CriticPolicy


def test_writer_capped_at_one_revision_round() -> None:
    policy = CriticPolicy()
    assert (
        policy.max_revision_rounds_overrides.get(
            "writer", policy.max_revision_rounds
        )
        == 1
    )


def test_coder_uses_default_two_rounds() -> None:
    policy = CriticPolicy()
    # Coder is intentionally NOT in the overrides map; .get falls through
    # to the default (2). Coder reruns are the highest-value revisions.
    assert (
        policy.max_revision_rounds_overrides.get(
            "coder", policy.max_revision_rounds
        )
        == 2
    )
    assert "coder" not in policy.max_revision_rounds_overrides


def test_modeler_uses_default_two_rounds() -> None:
    policy = CriticPolicy()
    assert (
        policy.max_revision_rounds_overrides.get(
            "modeler", policy.max_revision_rounds
        )
        == 2
    )
    assert "modeler" not in policy.max_revision_rounds_overrides


def test_analyzer_and_searcher_capped_at_one() -> None:
    policy = CriticPolicy()
    assert (
        policy.max_revision_rounds_overrides.get(
            "analyzer", policy.max_revision_rounds
        )
        == 1
    )
    assert (
        policy.max_revision_rounds_overrides.get(
            "searcher", policy.max_revision_rounds
        )
        == 1
    )


def test_unknown_agent_falls_through_to_default() -> None:
    policy = CriticPolicy()
    assert (
        policy.max_revision_rounds_overrides.get(
            "definitely_not_a_real_agent", policy.max_revision_rounds
        )
        == policy.max_revision_rounds
        == 2
    )


def test_overrides_default_is_immutable_mapping() -> None:
    # Matches the existing pattern for min_score_overrides — frozen=True
    # dataclasses can't safely hand out mutable dicts as default_factory output.
    policy = CriticPolicy()
    assert isinstance(policy.max_revision_rounds_overrides, MappingProxyType)


def test_default_policy_singleton_matches() -> None:
    # Sanity: DEFAULT_CRITIC_POLICY is the one the pipeline actually consults.
    assert DEFAULT_CRITIC_POLICY.max_revision_rounds_overrides["writer"] == 1
    assert DEFAULT_CRITIC_POLICY.max_revision_rounds == 2


def test_corrected_cost_estimates() -> None:
    # Round-6 audit: original estimates were 7× underestimate; this is the
    # corrected accounting (cap behavior unchanged at 1.00 RMB).
    policy = CriticPolicy()
    assert policy.estimated_review_cost_rmb == 0.14
    assert policy.estimated_revision_cost_rmb == 0.30
    assert policy.estimated_coder_revision_cost_rmb == 0.18
    assert policy.max_revision_cost_rmb == 1.00
