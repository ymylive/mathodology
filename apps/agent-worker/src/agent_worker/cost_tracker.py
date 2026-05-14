"""Per-run cost tracker — reads the gateway's authoritative running total.

Round-6 audit fix: the Critic's revision-cost budget used to be enforced
against fictitious ESTIMATED constants in `CriticPolicy` (0.02 RMB / review,
0.05 / revision, 0.12 / coder rerun) while real per-call costs are ~7x
larger. The loop could blow through `max_revision_cost_rmb=1.00` long
before triggering `budget_exhausted`.

This module is the reader-side fix: the gateway's `cost.rs` accumulator
INCRBYFLOATs `mm:cost:<run_id>` on every chargeable LLM call (mirroring
the Postgres `runs.cost_rmb` total). `RunCostTracker.get_total()` reads
that key — so we can compare the budget against ACTUAL spend, not
hand-waved estimates.

The tracker is read-only: the gateway is the source of truth for cost_rmb
and we never write to the key from the worker side. Missing key returns
0.0 (e.g. early in the run before any LLM call has been recorded); any
Redis error is swallowed and returns 0.0 so the budget check degrades
gracefully (the policy-level estimated check is still in force as a
backstop).
"""

from __future__ import annotations

import logging
from uuid import UUID

from redis.asyncio import Redis

_log = logging.getLogger(__name__)


def cost_key(run_id: UUID) -> str:
    """Redis key for the gateway-maintained running cost total in RMB.

    Mirrors `gateway::llm::cost::cost_key` (Rust) verbatim; if either
    side ever changes the format both must move in lockstep.
    """
    return f"mm:cost:{run_id}"


class RunCostTracker:
    """Reads the gateway's running cost total for one run.

    The gateway owns the cost accumulator (`crates/gateway/src/llm/cost.rs`):
    every `record_completion_cost` call both UPDATEs `runs.cost_rmb` in
    Postgres and INCRBYFLOATs `mm:cost:<run_id>` in Redis. This class is
    a thin reader for that Redis key.
    """

    def __init__(self, redis: Redis, run_id: UUID) -> None:
        self._redis = redis
        self._run_id = run_id
        self._cost_key = cost_key(run_id)
        self._baseline: float = 0.0

    async def get_total(self) -> float:
        """Return the run's total cost in RMB so far.

        Best-effort: returns 0.0 if the key is missing (very early in the
        run, before any LLM call has been recorded) or if Redis raises.
        Callers MUST treat the returned value as authoritative-when-nonzero
        and combine it with the policy-level estimate as a backstop.
        """
        try:
            val = await self._redis.get(self._cost_key)
        except Exception as exc:  # noqa: BLE001 — Redis transient errors
            _log.warning(
                "RunCostTracker: GET %s raised %s; returning 0.0", self._cost_key, exc
            )
            return 0.0
        if val is None:
            return 0.0
        try:
            # redis-py may return bytes or str depending on decode_responses;
            # tolerate both. INCRBYFLOAT serializes as ASCII float text.
            if isinstance(val, bytes):
                val = val.decode("ascii", errors="replace")
            return float(val)
        except (TypeError, ValueError) as exc:
            _log.warning(
                "RunCostTracker: %s value %r not parseable as float (%s); returning 0.0",
                self._cost_key,
                val,
                exc,
            )
            return 0.0

    async def snapshot_baseline(self) -> None:
        """Mark the current total as a baseline.

        Subsequent `delta_since_baseline()` returns just what's been spent
        since this call. Useful when the budget is scoped to a single
        Critic loop within a longer run.
        """
        self._baseline = await self.get_total()

    async def delta_since_baseline(self) -> float:
        """Return RMB spent since the most recent `snapshot_baseline()`.

        If `snapshot_baseline()` was never called the baseline is 0.0,
        so this returns the same value as `get_total()`.
        """
        total = await self.get_total()
        return total - self._baseline


__all__ = ["RunCostTracker", "cost_key"]
