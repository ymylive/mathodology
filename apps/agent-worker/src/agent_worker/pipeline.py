"""M5 pipeline: Analyzer → Coder, with Jupyter kernel lifecycle managed here.

The `done` event carries `notebook_path` so the gateway's audit task can
persist it on `runs.notebook_path` and the UI can offer a download link.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from mm_contracts import ProblemInput
from redis.asyncio import Redis

from agent_worker.agents import AgentError, AnalyzerAgent, CoderAgent
from agent_worker.config import get_settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.kernel import KernelSession


async def run_pipeline(redis: Redis, run_id: UUID, problem: ProblemInput) -> None:
    """Run Analyzer then Coder. Emit terminal `done` with status + notebook_path."""
    settings = get_settings()
    emitter = EventEmitter(redis, run_id)
    runs_dir = Path(settings.runs_dir).resolve()  # noqa: ASYNC240 — stdlib asyncio, not trio
    gateway = GatewayClient(settings.gateway_http, settings.dev_auth_token)
    kernel = KernelSession(run_id, runs_dir)

    try:
        analyzer = AnalyzerAgent(gateway, emitter)
        try:
            analysis = await analyzer.run_for_problem(problem)
        except AgentError:
            await emitter.emit("done", {"status": "failed"}, agent=None)
            return

        coder = CoderAgent(gateway, emitter, kernel)
        try:
            output = await coder.run(problem, analysis)
            await emitter.emit(
                "done",
                {"status": "success", "notebook_path": output.notebook_path},
                agent=None,
            )
        except AgentError:
            await emitter.emit("done", {"status": "failed"}, agent=None)
    finally:
        await gateway.close()
