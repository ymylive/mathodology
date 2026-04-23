"""Pipeline: Analyzer → Searcher → Modeler → Coder → Writer (M10, 5 agents).

The `done` event carries both `notebook_path` and `paper_path` so the
gateway's audit task can persist them and the UI can offer downloads.

M9 adds the HMML knowledge base: the Modeler consults a BM25-indexed library
of ~30 canonical modeling methods before producing its ModelSpec. The service
is loaded lazily once per process; if the seed dir is missing or empty the
Modeler transparently falls back to its pre-M9 behavior.

M10 inserts the Searcher between Analyzer and Modeler: it derives queries from
the Analyzer output, hits arXiv for prior work, and passes curated findings to
the Writer for Related Work / References. The Modeler is NOT affected (HMML
remains its only external context). If arXiv is unreachable the Searcher
degrades to an empty SearchFindings and the pipeline continues.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from mm_contracts import Figure, PaperDraft, ProblemInput
from redis.asyncio import Redis

from agent_worker.agents import (
    AgentError,
    AnalyzerAgent,
    CoderAgent,
    ModelerAgent,
    SearcherAgent,
    WriterAgent,
)
from agent_worker.config import get_settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.hmml import HMMLService
from agent_worker.kernel import KernelSession

_log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_hmml() -> HMMLService | None:
    """Load the HMML service once per process. Degrade to None on empty seed dir."""
    try:
        service = HMMLService.from_seed_dir()
    except Exception as e:  # noqa: BLE001 — any seed-load failure is non-fatal
        _log.warning("HMML seed load failed; Modeler will run without it: %s", e)
        return None
    if not service.methods:
        _log.warning("HMML seed dir is empty; Modeler will run without it.")
        return None
    return service


async def run_pipeline(redis: Redis, run_id: UUID, problem: ProblemInput) -> None:
    """Run the full 4-agent pipeline. Emit terminal `done` with paths + status."""
    settings = get_settings()
    emitter = EventEmitter(redis, run_id)
    runs_dir = Path(settings.runs_dir).resolve()  # noqa: ASYNC240 — stdlib asyncio, not trio
    run_dir = runs_dir / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    gateway = GatewayClient(settings.gateway_http, settings.dev_auth_token)
    kernel = KernelSession(run_id, runs_dir)
    hmml = _get_hmml()

    try:
        try:
            # Run-level reasoning effort threaded into every agent so each
            # `gateway.stream_completion` call carries the hint verbatim. Per-
            # agent PromptSpec overrides win on a call-by-call basis.
            kwargs: dict[str, Any] = {
                "run_effort": problem.reasoning_effort,
                "long_context": problem.long_context,
                "model_override": problem.model_override,
            }

            analyzer = AnalyzerAgent(gateway, emitter, **kwargs)
            analysis = await analyzer.run_for_problem(problem)

            searcher = SearcherAgent(gateway, emitter, **kwargs)
            findings = await searcher.run_for(problem, analysis)

            modeler = ModelerAgent(gateway, emitter, hmml=hmml, **kwargs)
            spec = await modeler.run_for(problem, analysis)

            coder = CoderAgent(gateway, emitter, kernel, **kwargs)
            coder_out = await coder.run(problem, analysis, spec)

            writer = WriterAgent(gateway, emitter, **kwargs)
            paper = await writer.run_for(
                problem, analysis, spec, coder_out, findings
            )

            # Resolve `[[FIG:<id>]]` placeholders in the Writer's output:
            # the on-disk paper.md gets real markdown image syntax (for the
            # preview UI), while paper.meta.json preserves placeholders for
            # the downstream LaTeX/DOCX/PDF exporter to render natively.
            paper_path = run_dir / "paper.md"
            paper_md = _render_paper_markdown(
                _substitute_figure_placeholders(paper, coder_out.figures, emitter_log=_log)
            )
            paper_path.write_text(paper_md, encoding="utf-8")  # noqa: ASYNC240

            meta_path = run_dir / "paper.meta.json"
            meta_path.write_text(
                json.dumps(
                    _build_paper_meta(problem, paper, coder_out.figures),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )  # noqa: ASYNC240

            # Do NOT include `cost_rmb` here: the gateway's cost.rs already
            # maintains runs.cost_rmb authoritatively from per-call cost events.
            # Setting cost_rmb=0 in the done payload would cause the audit task
            # to overwrite the correct accumulated total with zero.
            await emitter.emit(
                "done",
                {
                    "status": "success",
                    "notebook_path": coder_out.notebook_path,
                    "paper_path": str(paper_path),
                    "meta_path": str(meta_path),
                },
                agent=None,
            )
        except AgentError:
            await emitter.emit("done", {"status": "failed"}, agent=None)
    finally:
        await gateway.close()


_FIG_PLACEHOLDER_RE = re.compile(r"\[\[FIG:([a-z0-9_]+)\]\]")


def _substitute_figure_placeholders(
    paper: PaperDraft,
    figures: list[Figure],
    emitter_log: logging.Logger = _log,
) -> PaperDraft:
    """Return a copy of `paper` with `[[FIG:<id>]]` replaced by markdown images.

    Unknown ids are dropped with a warning so a single hallucinated reference
    doesn't leave broken markup in the preview. The figure list is the source
    of truth — the Coder shipped it, the Writer may only reference it.
    """
    by_id: dict[str, Figure] = {f.id: f for f in figures}

    def _repl(match: re.Match[str]) -> str:
        fig_id = match.group(1)
        fig = by_id.get(fig_id)
        if fig is None:
            emitter_log.warning(
                "Writer referenced unknown figure id %r; placeholder dropped",
                fig_id,
            )
            return ""
        # Blank lines around the image so the markdown renderer always treats
        # it as a block, regardless of the surrounding paragraph.
        return f"\n\n![{fig.caption}]({fig.path_png})\n\n*图: {fig.caption}*\n\n"

    new_sections = [
        type(s)(
            title=s.title,
            body_markdown=_FIG_PLACEHOLDER_RE.sub(_repl, s.body_markdown),
        )
        for s in paper.sections
    ]
    return PaperDraft(
        title=paper.title,
        abstract=paper.abstract,
        sections=new_sections,
        references=paper.references,
        figure_refs=paper.figure_refs,
    )


def _build_paper_meta(
    problem: ProblemInput,
    paper: PaperDraft,
    figures: list[Figure],
) -> dict[str, Any]:
    """Structured export payload consumed by the gateway's PDF/DOCX/LaTeX path.

    Sections keep the raw `[[FIG:<id>]]` placeholders so the exporter can
    render each figure natively (e.g. `\\includegraphics[width=0.8\\textwidth]`
    for LaTeX) instead of parsing markdown image syntax.
    """
    return {
        "title": paper.title,
        "abstract": paper.abstract,
        "competition_type": problem.competition_type,
        "problem_text": problem.problem_text,
        "sections": [
            {"title": s.title, "body_markdown": s.body_markdown}
            for s in paper.sections
        ],
        "references": list(paper.references),
        "figures": [f.model_dump(mode="json") for f in figures],
    }


def _render_paper_markdown(paper: PaperDraft) -> str:
    """Render a PaperDraft to a Markdown document string."""
    parts: list[str] = [f"# {paper.title}", "", "## Abstract", "", paper.abstract]
    for section in paper.sections:
        parts.extend(["", f"## {section.title}", "", section.body_markdown])
    if paper.references:
        parts.extend(["", "## References", ""])
        for i, ref in enumerate(paper.references, start=1):
            parts.append(f"{i}. {ref}")
    # Ensure trailing newline for POSIX-friendly files.
    return "\n".join(parts) + "\n"


__all__ = ["run_pipeline"]
