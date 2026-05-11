"""Writer agent: synthesise a publication-grade paper draft.

Final stage of the pipeline. Single LLM call, structured JSON output → PaperDraft.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mm_contracts import (
    AnalyzerOutput,
    CoderOutput,
    ModelSpec,
    PaperDraft,
    ProblemInput,
    ReasoningEffort,
    SearchFindings,
)

from agent_worker.agents.base import BaseAgent
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient

# Per-paper Writer-side budget. Last-resort safety net: Searcher already
# compacts files > 24k chars in Phase 3.6, but uncompacted survivors get
# truncated here at a paragraph boundary so the prompt cannot explode.
WRITER_SOFT_TRUNCATE_CHARS = 32_000


class WriterAgent(BaseAgent):
    """Final stage of the pipeline. Produces a `PaperDraft`."""

    AGENT_NAME = "writer"
    OUTPUT_MODEL = PaperDraft

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "medium",
        long_context: bool = False,
        model_override: str | None = None,
        run_dir: Path | None = None,
    ) -> None:
        super().__init__(
            gateway,
            emitter,
            prompt_version=prompt_version,
            run_effort=run_effort,
            long_context=long_context,
            model_override=model_override,
        )
        # Run-directory root (e.g. runs/<run_id>) used to resolve relative
        # paths from `SearchFindings.paper_fulltext_paths`. Optional so
        # existing callers / tests that don't pass it still work.
        self._run_dir: Path | None = run_dir

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
        findings_payload: dict[str, Any] = (
            findings.model_dump(mode="json")
            if findings is not None
            else {"queries": [], "papers": [], "key_findings": [], "datasets_mentioned": []}
        )
        fulltexts_block = self._build_fulltexts_block(findings)
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
            # Pass rich figure dicts (id + caption + paths) so the Writer can
            # emit `[[FIG:<id>]]` placeholders that the pipeline substitutes.
            # Fall back to legacy path list if the Coder produced no
            # structured figures (e.g. old notebooks / mock tests).
            coder_figures=json.dumps(
                [f.model_dump(mode="json") for f in coder_output.figures]
                if coder_output.figures
                else coder_output.figure_paths,
                ensure_ascii=False,
            ),
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
            paper_fulltexts=fulltexts_block,
        )
        assert isinstance(output, PaperDraft)
        return output

    def _build_fulltexts_block(
        self, findings: SearchFindings | None
    ) -> str:
        """Read paper full-text files referenced by `findings.paper_fulltext_paths`.

        Each path is resolved against `self._run_dir`. Missing files /
        read errors are skipped silently so the pipeline degrades to
        abstract-only citation. Files larger than
        `WRITER_SOFT_TRUNCATE_CHARS` are truncated at the last paragraph
        boundary before the cap as a last safety net.
        """
        if (
            findings is None
            or not findings.paper_fulltext_paths
            or self._run_dir is None
        ):
            return ""
        blocks: list[str] = []
        for idx, rel_path in enumerate(findings.paper_fulltext_paths, start=1):
            abs_path = self._run_dir / rel_path
            if not abs_path.exists():
                continue
            try:
                text = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if len(text) > WRITER_SOFT_TRUNCATE_CHARS:
                text = (
                    text[:WRITER_SOFT_TRUNCATE_CHARS].rsplit("\n\n", 1)[0]
                    + "\n\n[...truncated]"
                )
            blocks.append(
                f"### Paper {idx} (cite as findings.papers[{idx - 1}])\n\n{text}"
            )
        return "\n\n".join(blocks)


__all__ = ["WriterAgent"]
