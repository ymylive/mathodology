"""Critic agent: review one upstream artifact and produce a CritiqueReport."""

from __future__ import annotations

import json
from typing import Any, Literal

from mm_contracts import CriticRole, CritiqueReport, ReasoningEffort

from agent_worker.agents.base import BaseAgent
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient

ReviewTarget = Literal["analyzer", "searcher", "modeler", "coder", "writer"]

DEFAULT_ROLES: dict[ReviewTarget, list[CriticRole]] = {
    "analyzer": ["modeling_coach", "academic_reviewer"],
    "searcher": ["academic_reviewer"],
    "modeler": ["modeling_coach", "academic_reviewer"],
    "coder": ["modeling_coach", "code_reviewer"],
    "writer": ["academic_reviewer", "modeling_coach"],
}

DEFAULT_CHECKLIST_IDS: dict[ReviewTarget, list[str]] = {
    "analyzer": [
        "subquestion_coverage",
        "assumption_quality",
        "data_requirements",
        "approach_usability",
    ],
    "searcher": [
        "source_quality",
        "citation_coverage",
        "relevance",
        "empty_result_handling",
    ],
    "modeler": [
        "method_fit",
        "equation_consistency",
        "coder_executability",
        "validation_strategy",
    ],
    "coder": [
        "execution_support",
        "numerical_results",
        "validation_sensitivity",
        "figure_registration",
    ],
    "writer": [
        "award_abstract",
        "subquestion_answers",
        "sensitivity_discussion",
        "references",
        "figure_discussion",
        "anonymity",
    ],
}


class CriticAgent(BaseAgent):
    """Reviews one artifact against explicit criteria."""

    AGENT_NAME = "critic"
    OUTPUT_MODEL = CritiqueReport

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "medium",
        long_context: bool = False,
        model_override: str | None = None,
    ) -> None:
        super().__init__(
            gateway,
            emitter,
            prompt_version,
            run_effort=run_effort,
            long_context=long_context,
            model_override=model_override,
        )

    async def review(
        self,
        *,
        target_agent: ReviewTarget,
        target_schema: str,
        artifact: dict[str, Any],
        context: dict[str, Any],
        criteria: list[str],
        revision_round: int = 0,
        max_revision_rounds: int = 0,
        roles: list[CriticRole] | None = None,
    ) -> CritiqueReport:
        selected_roles = roles or DEFAULT_ROLES[target_agent]
        output = await self.run(
            target_agent=target_agent,
            target_schema=target_schema,
            artifact_json=json.dumps(artifact, ensure_ascii=False, indent=2),
            context_json=json.dumps(context, ensure_ascii=False, indent=2),
            criteria="\n".join(f"- {item}" for item in criteria),
            roles="\n".join(f"- {role}" for role in selected_roles),
            checklist="\n".join(
                f"- {self._checklist_id_for(target_agent, idx, item)}: {item}"
                for idx, item in enumerate(criteria)
            ),
            revision_round=revision_round,
            max_revision_rounds=max_revision_rounds,
        )
        assert isinstance(output, CritiqueReport)
        return output

    @staticmethod
    def _checklist_id_for(target_agent: ReviewTarget, idx: int, criteria: str) -> str:
        ids = DEFAULT_CHECKLIST_IDS[target_agent]
        if idx < len(ids):
            return ids[idx]
        return CriticAgent._criteria_to_checklist_id(criteria)

    @staticmethod
    def _criteria_to_checklist_id(criteria: str) -> str:
        slug = "".join(
            ch.lower() if ch.isalnum() else "_" for ch in criteria.strip()
        ).strip("_")
        stop_words = {"a", "an", "and", "or", "the", "to", "of", "by", "with"}
        parts = [part for part in slug.split("_") if part and part not in stop_words]
        return "_".join(parts[:4]) or "criterion"


__all__ = ["CriticAgent", "ReviewTarget"]
