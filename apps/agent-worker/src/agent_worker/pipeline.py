"""M6 pipeline: Analyzer → Modeler → Coder → Writer.

The `done` event carries both `notebook_path` and `paper_path` so the
gateway's audit task can persist them and the UI can offer downloads.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from mm_contracts import PaperDraft, ProblemInput
from redis.asyncio import Redis

from agent_worker.agents import (
    AgentError,
    AnalyzerAgent,
    CoderAgent,
    ModelerAgent,
    WriterAgent,
)
from agent_worker.config import get_settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.kernel import KernelSession


async def run_pipeline(redis: Redis, run_id: UUID, problem: ProblemInput) -> None:
    """Run the full 4-agent pipeline. Emit terminal `done` with paths + status."""
    settings = get_settings()
    emitter = EventEmitter(redis, run_id)
    runs_dir = Path(settings.runs_dir).resolve()  # noqa: ASYNC240 — stdlib asyncio, not trio
    run_dir = runs_dir / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    gateway = GatewayClient(settings.gateway_http, settings.dev_auth_token)
    kernel = KernelSession(run_id, runs_dir)

    try:
        try:
            analyzer = AnalyzerAgent(gateway, emitter)
            analysis = await analyzer.run_for_problem(problem)

            modeler = ModelerAgent(gateway, emitter)
            spec = await modeler.run_for(problem, analysis)

            coder = CoderAgent(gateway, emitter, kernel)
            coder_out = await coder.run(problem, analysis, spec)

            writer = WriterAgent(gateway, emitter)
            paper = await writer.run_for(problem, analysis, spec, coder_out)

            # Write paper.md to disk.
            paper_path = run_dir / "paper.md"
            paper_md = _render_paper_markdown(paper)
            paper_path.write_text(paper_md, encoding="utf-8")  # noqa: ASYNC240

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
                },
                agent=None,
            )
        except AgentError:
            await emitter.emit("done", {"status": "failed"}, agent=None)
    finally:
        await gateway.close()


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
