"""Critic agent: review one upstream artifact and produce a CritiqueReport."""

from __future__ import annotations

import json
from typing import Any, Literal

from mm_contracts import CritiqueReport, ReasoningEffort

from agent_worker.agents.base import BaseAgent
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient

ReviewTarget = Literal["analyzer", "modeler", "coder", "writer"]


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
    ) -> CritiqueReport:
        output = await self.run(
            target_agent=target_agent,
            target_schema=target_schema,
            artifact_json=json.dumps(artifact, ensure_ascii=False, indent=2),
            context_json=json.dumps(context, ensure_ascii=False, indent=2),
            criteria="\n".join(f"- {item}" for item in criteria),
        )
        assert isinstance(output, CritiqueReport)
        return output


__all__ = ["CriticAgent", "ReviewTarget"]
