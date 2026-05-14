"""Mid-run cancellation signal — sourcemap RemoteControlHandle pattern.

The gateway writes `mm:cancel:<run_id> = "1"` on `POST /runs/:id/cancel`.
The worker pipeline polls this key between stages (and at each Coder
turn) via `CancellationChecker.check_or_raise()`. When set, we raise
`RunCancelled` which `run_pipeline` catches and converts to a clean
`done(status="cancelled")` event with whatever partial artifacts exist.

Why a key (not a Pub/Sub channel): the worker only checks at well-defined
boundaries (between stages); a fire-and-forget pub/sub would either need
state or risk losing the signal if it arrives during an LLM stream. A
key persists for an hour (TTL set by gateway), so the next stage's
boundary check finds it deterministically.
"""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis

from agent_worker.agents.base import AgentError


class RunCancelled(AgentError):
    """Raised by `CancellationChecker.check_or_raise()` after the gateway
    signalled cancel. Caught by `run_pipeline` and converted to a
    `done(status="cancelled")` event.
    """


class CancellationChecker:
    """Reads `mm:cancel:<run_id>` to decide whether to halt.

    Cheap (one Redis GET) — safe to call at every stage boundary, and
    every Coder turn. We deliberately do NOT poll inside an LLM stream:
    cancelling mid-token would orphan the gateway's billing state and
    leave the prompt cache in a broken half-cached state. Stage-boundary
    granularity is good enough for users.
    """

    def __init__(self, redis: Redis, run_id: UUID) -> None:
        self._redis = redis
        self._run_id = run_id
        self._key = f"mm:cancel:{run_id}"

    async def is_cancelled(self) -> bool:
        try:
            val = await self._redis.get(self._key)
        except Exception:  # noqa: BLE001 — never let the check itself fail the run
            return False
        return bool(val)

    async def check_or_raise(self) -> None:
        """Raise `RunCancelled` if the cancel flag is set; otherwise return."""
        if await self.is_cancelled():
            raise RunCancelled(f"run {self._run_id} cancelled by user")


__all__ = ["CancellationChecker", "RunCancelled"]
