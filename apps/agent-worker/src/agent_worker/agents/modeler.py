"""Modeler agent: pick ONE concrete modeling approach and fully spec it.

Runs between Analyzer and Coder. Single LLM call, structured JSON output →
ModelSpec. Optionally consults the HMML knowledge base first and surfaces the
top retrieved methods in the prompt so the LLM can anchor on canonical
techniques rather than invent from scratch.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mm_contracts import AnalyzerOutput, ModelSpec, ProblemInput, ReasoningEffort

from agent_worker.agents.base import BaseAgent
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient

if TYPE_CHECKING:
    from mm_contracts import MethodNode

    from agent_worker.hmml import HMMLService


class ModelerAgent(BaseAgent):
    """Second stage of the pipeline. Produces a `ModelSpec`."""

    AGENT_NAME = "modeler"
    OUTPUT_MODEL = ModelSpec

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        hmml: HMMLService | None = None,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "medium",
    ) -> None:
        super().__init__(gateway, emitter, prompt_version, run_effort=run_effort)
        self.hmml = hmml

    async def run_for(
        self, problem: ProblemInput, analysis: AnalyzerOutput
    ) -> ModelSpec:
        """Render the template from problem+analysis (+HMML context) and call the LLM."""
        retrieved: list[tuple[MethodNode, float]] = []
        if self.hmml is not None:
            query = (
                f"{problem.problem_text}\n\n"
                f"{analysis.restated_problem}\n"
                + " ".join(analysis.sub_questions)
            )
            retrieved = self.hmml.retrieve_hybrid(query, top_k=5)
            if retrieved:
                await self.emitter.emit(
                    "log",
                    {
                        "level": "info",
                        "message": (
                            "HMML retrieved: "
                            + ", ".join(m.name for m, _ in retrieved)
                        ),
                    },
                    agent=self.AGENT_NAME,
                )

        retrieved_ctx = self._render_retrieved(retrieved)
        output = await self.run(
            problem_text=problem.problem_text,
            competition_type=problem.competition_type,
            analysis_json=json.dumps(
                analysis.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            retrieved_methods=retrieved_ctx,
        )
        assert isinstance(output, ModelSpec)
        return output

    def _render_retrieved(
        self, retrieved: list[tuple[MethodNode, float]]
    ) -> str:
        """Format the retrieved methods as a Markdown block for the user prompt."""
        if not retrieved:
            return "(HMML knowledge base unavailable — use your own judgment.)"
        blocks: list[str] = []
        for m, score in retrieved:
            pitfalls = "; ".join(m.common_pitfalls) or "(none listed)"
            blocks.append(
                f"### {m.name} (id: {m.id}, score: {score:.3f})\n"
                f"- Domain: {m.domain} / {m.subdomain}\n"
                f"- Applicable: {'; '.join(m.applicable_scenarios)}\n"
                f"- Canonical form: ${m.math_form}$\n"
                f"- Common pitfalls: {pitfalls}\n"
            )
        return "\n".join(blocks)


__all__ = ["ModelerAgent"]
