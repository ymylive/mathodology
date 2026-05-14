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

from agent_worker.agents._common import (
    problem_letter_from_problem_text as _problem_letter_from_problem_text,
)
from agent_worker.agents.base import BaseAgent
from agent_worker.events import EventEmitter
from agent_worker.few_shot import FewShotLibrary, format_writer_block, get_default_library
from agent_worker.gateway_client import GatewayClient

# Per-paper Writer-side budget. Last-resort safety net: Searcher already
# compacts files > 24k chars in Phase 3.6, but uncompacted survivors get
# truncated here at a paragraph boundary so the prompt cannot explode.
WRITER_SOFT_TRUNCATE_CHARS = 32_000

# How many same-problem-type exemplars to inject into the Writer prompt.
# 2 is a good trade-off: enough variety to anchor structure, not so many
# that we blow past token_budget_in or risk style copying.
WRITER_FEW_SHOT_K = 2

_ZH_FAMILIES = {"cumcm", "huashu", "国赛"}


def _writer_language(competition_type: str) -> str:
    s = (competition_type or "").lower()
    if any(token in s for token in _ZH_FAMILIES):
        return "zh"
    if "华数" in (competition_type or ""):
        return "zh"
    return "en"


class WriterAgent(BaseAgent):
    """Final stage of the pipeline. Produces a `PaperDraft`."""

    AGENT_NAME = "writer"
    OUTPUT_MODEL = PaperDraft

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "high",
        long_context: bool = False,
        model_override: str | None = None,
        run_dir: Path | None = None,
        few_shot_library: FewShotLibrary | None = None,
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
        # Process-singleton few-shot library by default. Tests can inject a
        # custom one (e.g. an empty library) to keep prompts deterministic.
        self._few_shot: FewShotLibrary = (
            few_shot_library
            if few_shot_library is not None
            else get_default_library()
        )

    async def run_for(
        self,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        spec: ModelSpec,
        coder_output: CoderOutput,
        findings: SearchFindings | None = None,
        upstream_reminders: str = "",
    ) -> PaperDraft:
        """Render the Writer template from all upstream artifacts and call the LLM."""
        # Fallback so existing callers / tests that don't pass findings still work.
        findings_payload: dict[str, Any] = (
            findings.model_dump(mode="json")
            if findings is not None
            else {"queries": [], "papers": [], "key_findings": [], "datasets_mentioned": []}
        )
        fulltexts_block = self._build_fulltexts_block(findings)
        # Award-winning exemplars: top-K winning-paper Summary excerpts from
        # the same competition family + problem letter. Empty when the index
        # isn't built yet — Writer falls back to its baseline behaviour.
        language = _writer_language(problem.competition_type)
        letter = _problem_letter_from_problem_text(problem.problem_text)
        exemplars = self._few_shot.top_k(
            competition_type=problem.competition_type,
            problem_letter=letter,
            k=WRITER_FEW_SHOT_K,
        )
        few_shot_block = format_writer_block(exemplars, language=language)
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
            # Source removed in round-7 cost optimization — Writer doesn't need
            # the code, only the outputs. Saves ~30% of Writer prompt input.
            coder_cells=json.dumps(
                [
                    {
                        "index": c.index,
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
            few_shot_exemplars=few_shot_block,
            upstream_reminders=upstream_reminders,
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
