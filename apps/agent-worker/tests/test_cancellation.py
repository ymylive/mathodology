"""Tests for the mid-run cancellation signal."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agent_worker.cancellation import CancellationChecker, RunCancelled


async def test_is_cancelled_returns_false_when_key_missing() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    chk = CancellationChecker(redis, uuid4())
    assert await chk.is_cancelled() is False


async def test_is_cancelled_returns_true_when_key_set() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1")
    chk = CancellationChecker(redis, uuid4())
    assert await chk.is_cancelled() is True


async def test_check_or_raise_is_noop_when_not_cancelled() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    chk = CancellationChecker(redis, uuid4())
    # Must NOT raise
    await chk.check_or_raise()


async def test_check_or_raise_raises_RunCancelled_when_set() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1")
    run_id = uuid4()
    chk = CancellationChecker(redis, run_id)
    with pytest.raises(RunCancelled) as exc:
        await chk.check_or_raise()
    assert str(run_id) in str(exc.value)


async def test_redis_get_error_is_swallowed() -> None:
    """A Redis hiccup must NEVER fail the run — the check is best-effort.

    Worst case: a real cancel signal is missed, the run keeps going. Better
    than the inverse (false-positive cancel from a Redis blip).
    """
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=ConnectionError("redis flaky"))
    chk = CancellationChecker(redis, uuid4())
    assert await chk.is_cancelled() is False


async def test_run_cancelled_is_agent_error_subclass() -> None:
    """Pipeline catches `AgentError` at the top — RunCancelled must
    funnel through that catch block to land in the clean done(failed)
    path, then be re-classified to done(status='cancelled')."""
    from agent_worker.agents.base import AgentError

    assert issubclass(RunCancelled, AgentError)


async def test_check_or_raise_uses_run_specific_key() -> None:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    run_id = uuid4()
    chk = CancellationChecker(redis, run_id)
    await chk.is_cancelled()
    redis.get.assert_awaited_once_with(f"mm:cancel:{run_id}")
