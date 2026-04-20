"""Modeler agent: pick ONE concrete modeling approach and fully spec it.

Runs between Analyzer and Coder. Single LLM call, structured JSON output → ModelSpec.
"""

from __future__ import annotations

import json

from mm_contracts import AnalyzerOutput, ModelSpec, ProblemInput

from agent_worker.agents.base import BaseAgent


class ModelerAgent(BaseAgent):
    """Second stage of the pipeline. Produces a `ModelSpec`."""

    AGENT_NAME = "modeler"
    OUTPUT_MODEL = ModelSpec

    async def run_for(
        self, problem: ProblemInput, analysis: AnalyzerOutput
    ) -> ModelSpec:
        """Render the template from problem+analysis and call the LLM."""
        output = await self.run(
            problem_text=problem.problem_text,
            competition_type=problem.competition_type,
            analysis_json=json.dumps(
                analysis.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
        )
        assert isinstance(output, ModelSpec)
        return output


__all__ = ["ModelerAgent"]
