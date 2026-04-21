"""Writer agent: synthesise a publication-grade paper draft.

Final stage of the pipeline. Single LLM call, structured JSON output → PaperDraft.
"""

from __future__ import annotations

import json

from mm_contracts import (
    AnalyzerOutput,
    CoderOutput,
    ModelSpec,
    PaperDraft,
    ProblemInput,
    SearchFindings,
)

from agent_worker.agents.base import BaseAgent


class WriterAgent(BaseAgent):
    """Final stage of the pipeline. Produces a `PaperDraft`."""

    AGENT_NAME = "writer"
    OUTPUT_MODEL = PaperDraft

    async def run_for(
        self,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        spec: ModelSpec,
        coder_output: CoderOutput,
        findings: SearchFindings | None = None,
    ) -> PaperDraft:
        """Render the Writer template from all upstream artifacts and call the LLM."""
        # Fallback so existing callers / tests that don't pass findings still work.
        findings_payload = (
            findings.model_dump(mode="json")
            if findings is not None
            else {"queries": [], "papers": [], "key_findings": [], "datasets_mentioned": []}
        )
        output = await self.run(
            problem_text=problem.problem_text,
            competition_type=problem.competition_type,
            analysis_json=json.dumps(
                analysis.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            spec_json=json.dumps(
                spec.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            coder_summary=coder_output.final_summary,
            coder_figures=json.dumps(coder_output.figure_paths),
            coder_cells=json.dumps(
                [
                    {
                        "source": c.source,
                        "stdout": c.stdout[:500],
                        "result": c.result_text,
                    }
                    for c in coder_output.cells
                ],
                ensure_ascii=False,
                indent=2,
            ),
            findings_json=json.dumps(
                findings_payload, ensure_ascii=False, indent=2
            ),
        )
        assert isinstance(output, PaperDraft)
        return output


__all__ = ["WriterAgent"]
