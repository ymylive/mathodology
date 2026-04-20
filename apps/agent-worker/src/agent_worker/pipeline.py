"""M1 fake pipeline: emits three events and returns."""

from __future__ import annotations

import asyncio
from uuid import UUID

from mm_contracts import ProblemInput
from redis.asyncio import Redis

from agent_worker.events import EventEmitter


async def run_pipeline(redis: Redis, run_id: UUID, problem: ProblemInput) -> None:
    """Minimal three-event pipeline for M1.

    Emits `stage.start(analyzer)`, `stage.start(modeler)`, then `done`.
    No real work — this is a wiring test from gateway → worker → WS.
    """
    del problem  # unused in M1
    emitter = EventEmitter(redis, run_id)

    await emitter.emit("stage.start", {"stage": "analyzer"}, agent="analyzer")
    await asyncio.sleep(0.2)

    await emitter.emit("stage.start", {"stage": "modeler"}, agent="modeler")
    await asyncio.sleep(0.2)

    await emitter.emit("done", {"status": "success"}, agent=None)
