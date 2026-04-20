"""M4 pipeline: run the Analyzer agent, then emit `done`."""

from __future__ import annotations

from uuid import UUID

from mm_contracts import ProblemInput
from redis.asyncio import Redis

from agent_worker.agents import AgentError, AnalyzerAgent
from agent_worker.config import get_settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient


async def run_pipeline(redis: Redis, run_id: UUID, problem: ProblemInput) -> None:
    """Run the pipeline for a single problem.

    M4 only wires up the Analyzer stage. Subsequent milestones will chain
    Modeler → Coder → Writer. The `done` event's status reflects whether the
    agent chain succeeded; per-stage errors are emitted by `BaseAgent`.
    """
    settings = get_settings()
    emitter = EventEmitter(redis, run_id)
    gateway = GatewayClient(settings.gateway_http, settings.dev_auth_token)

    try:
        analyzer = AnalyzerAgent(gateway, emitter)
        try:
            await analyzer.run_for_problem(problem)
            await emitter.emit("done", {"status": "success"}, agent=None)
        except AgentError:
            # BaseAgent already emitted the stage `error`; mark the run failed.
            await emitter.emit("done", {"status": "failed"}, agent=None)
    finally:
        await gateway.close()
