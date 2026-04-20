"""Analyzer agent: scope the problem, surface assumptions, sketch approaches."""

from __future__ import annotations

from mm_contracts import AnalyzerOutput, ProblemInput

from agent_worker.agents.base import BaseAgent


class AnalyzerAgent(BaseAgent):
    """First stage of the pipeline. Produces an `AnalyzerOutput`."""

    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput

    async def run_for_problem(self, problem: ProblemInput) -> AnalyzerOutput:
        """Render the template from a `ProblemInput` and call the LLM."""
        output = await self.run(
            problem_text=problem.problem_text,
            competition_type=problem.competition_type,
            attachments_summary=self._summarize_attachments(problem),
        )
        # `run` returns `BaseModel`; narrow to the concrete model for callers.
        assert isinstance(output, AnalyzerOutput)
        return output

    @staticmethod
    def _summarize_attachments(p: ProblemInput) -> str:
        if not p.attachments:
            return "(none)"
        return "\n".join(f"- {a.name} ({a.kind})" for a in p.attachments)


__all__ = ["AnalyzerAgent"]
