"""Tests for `RunCostTracker` — the reader of the gateway's `mm:cost:<run_id>`.

The gateway side (`crates/gateway/src/llm/cost.rs::record_completion_cost`)
INCRBYFLOATs that key on every chargeable LLM call. We don't exercise the
gateway path here (it's covered separately); these tests fake Redis so the
tracker's behavior — including graceful degradation when the key is missing
or Redis raises — is locked down independently of the gateway build.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from agent_worker.cost_tracker import RunCostTracker, cost_key


def _make_redis(get_return_value: object) -> AsyncMock:
    """Build an AsyncMock that mimics redis.asyncio.Redis.get()."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=get_return_value)
    return redis


@pytest.fixture
def run_id() -> UUID:
    return uuid4()


def test_cost_key_format_matches_gateway(run_id: UUID) -> None:
    """The key format MUST stay locked to the gateway's Rust `cost_key`.

    If this assertion breaks the worker can't read the gateway-maintained
    total and the budget regresses to estimate-only behavior.
    """
    assert cost_key(run_id) == f"mm:cost:{run_id}"


async def test_get_total_returns_zero_when_key_missing(run_id: UUID) -> None:
    redis = _make_redis(None)
    tracker = RunCostTracker(redis, run_id)

    assert await tracker.get_total() == 0.0
    redis.get.assert_awaited_once_with(f"mm:cost:{run_id}")


async def test_get_total_reads_redis_value(run_id: UUID) -> None:
    redis = _make_redis("0.42")
    tracker = RunCostTracker(redis, run_id)

    assert await tracker.get_total() == pytest.approx(0.42)


async def test_get_total_handles_bytes_payload(run_id: UUID) -> None:
    """INCRBYFLOAT replies are ASCII text; redis-py without decode_responses
    surfaces them as bytes. The tracker must handle both transparently."""
    redis = _make_redis(b"1.23")
    tracker = RunCostTracker(redis, run_id)

    assert await tracker.get_total() == pytest.approx(1.23)


async def test_get_total_resilient_to_redis_error(run_id: UUID) -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=RuntimeError("connection lost"))
    tracker = RunCostTracker(redis, run_id)

    # MUST NOT raise — budget enforcement degrades to estimate fallback
    # at the call site, but the tracker contract is "never raise".
    assert await tracker.get_total() == 0.0


async def test_get_total_resilient_to_unparseable_value(run_id: UUID) -> None:
    redis = _make_redis("not-a-float")
    tracker = RunCostTracker(redis, run_id)

    assert await tracker.get_total() == 0.0


async def test_snapshot_baseline_resets_delta(run_id: UUID) -> None:
    redis = _make_redis("0.50")
    tracker = RunCostTracker(redis, run_id)

    await tracker.snapshot_baseline()
    # After snapshot at 0.50, delta is 0.0
    assert await tracker.delta_since_baseline() == pytest.approx(0.0)


async def test_delta_since_baseline_after_increment(run_id: UUID) -> None:
    """Simulate Redis values rising as the gateway records more LLM calls."""
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=["0.20", "0.20", "0.75"])
    tracker = RunCostTracker(redis, run_id)

    # call 1: snapshot at 0.20 (one GET)
    await tracker.snapshot_baseline()
    # call 2: still at 0.20 → delta 0.0
    assert await tracker.delta_since_baseline() == pytest.approx(0.0)
    # call 3: now at 0.75 → delta 0.55
    assert await tracker.delta_since_baseline() == pytest.approx(0.55)


async def test_delta_without_explicit_baseline_equals_total(run_id: UUID) -> None:
    """If `snapshot_baseline()` is never called the baseline is 0.0 so
    `delta_since_baseline()` returns the same value as `get_total()`."""
    redis = _make_redis("0.30")
    tracker = RunCostTracker(redis, run_id)

    assert await tracker.delta_since_baseline() == pytest.approx(0.30)
