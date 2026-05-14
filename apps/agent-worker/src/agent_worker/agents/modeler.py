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
from agent_worker.few_shot import FewShotLibrary, format_writer_block, get_default_library
from agent_worker.gateway_client import GatewayClient

if TYPE_CHECKING:
    from mm_contracts import MethodNode

    from agent_worker.hmml import HMMLService

# Modeler benefits from seeing >1 prior approach to anchor its own
# "compare 2 candidates" rule, but keep this lean to preserve token budget.
MODELER_FEW_SHOT_K = 2

_ZH_FAMILIES = {"cumcm", "huashu", "国赛"}


def _modeler_language(competition_type: str) -> str:
    s = (competition_type or "").lower()
    if any(token in s for token in _ZH_FAMILIES) or "华数" in (competition_type or ""):
        return "zh"
    return "en"


def _problem_letter_from_problem_text(problem_text: str) -> str | None:
    import re

    if not problem_text:
        return None
    m = re.search(r"Problem\s+([A-F])\b", problem_text, flags=re.IGNORECASE)
    return m.group(1).upper() if m else None


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
        long_context: bool = False,
        model_override: str | None = None,
        few_shot_library: FewShotLibrary | None = None,
    ) -> None:
        super().__init__(
            gateway,
            emitter,
            prompt_version,
            run_effort=run_effort,
            long_context=long_context,
            model_override=model_override,
        )
        self.hmml = hmml
        self._few_shot: FewShotLibrary = (
            few_shot_library
            if few_shot_library is not None
            else get_default_library()
        )

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
        # Same-problem-type winning-paper exemplars — gives the LLM concrete
        # anchor patterns for "chosen approach + rationale" that judges have
        # historically rewarded. Missing index → empty block, silent fallback.
        language = _modeler_language(problem.competition_type)
        letter = _problem_letter_from_problem_text(problem.problem_text)
        few_shot_block = format_writer_block(
            self._few_shot.top_k(
                competition_type=problem.competition_type,
                problem_letter=letter,
                k=MODELER_FEW_SHOT_K,
            ),
            language=language,
        )
        output = await self.run(
            problem_text=problem.problem_text,
            competition_type=problem.competition_type,
            analysis_json=json.dumps(
                analysis.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            retrieved_methods=retrieved_ctx,
            few_shot_exemplars=few_shot_block,
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
