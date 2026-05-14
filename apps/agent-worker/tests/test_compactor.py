"""Tests for the shared text-compaction utility.

Pytest runs in ``asyncio_mode = "auto"`` (see ``pyproject.toml``), so plain
``async def test_*`` functions are picked up without explicit markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_worker.compactor import (
    NO_TOOLS_PREAMBLE,
    CompactionPolicy,
    Compactor,
    PaperCompactor,
)


@dataclass
class _StubCaller:
    """Records each call and returns either a queued string or raises."""

    # Either a list of strings (one consumed per call) or a single exception
    # raised on every call. `responses` rotates; once exhausted the last
    # response is sticky.
    responses: list[str] = field(default_factory=list)
    raise_exc: Exception | None = None
    calls: list[dict[str, str]] = field(default_factory=list)

    async def __call__(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        if self.raise_exc is not None:
            raise self.raise_exc
        if not self.responses:
            return ""
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _policy(**overrides: object) -> CompactionPolicy:
    """Default test policy: small thresholds so we don't have to fabricate 24k."""
    base = {
        "threshold_chars": 100,
        "target_chars": 80,
        "min_output_chars": 20,
        "max_consecutive_failures": 3,
    }
    base.update(overrides)  # type: ignore[arg-type]
    return CompactionPolicy(**base)  # type: ignore[arg-type]


async def test_under_threshold_skips_compaction() -> None:
    caller = _StubCaller(responses=["UNUSED"])
    c = Compactor(caller, policy=_policy(threshold_chars=1_000))
    result = await c.compact("short text")
    assert result.was_compacted is False
    assert result.compacted == "short text"
    assert result.original_chars == len("short text")
    assert result.compacted_chars == len("short text")
    assert result.failure_reason == "below_threshold"
    assert caller.calls == []  # LLM was NOT invoked


async def test_above_threshold_calls_llm_and_returns_summary() -> None:
    summary = "x" * 40  # well above min_output_chars(20), below original size
    caller = _StubCaller(responses=[summary])
    c = Compactor(caller, model="gpt-test", policy=_policy())
    long_input = "y" * 500
    result = await c.compact(long_input)
    assert result.was_compacted is True
    assert result.compacted == summary
    assert result.original_chars == 500
    assert result.compacted_chars == 40
    assert result.failure_reason is None
    assert len(caller.calls) == 1
    call = caller.calls[0]
    assert call["model"] == "gpt-test"
    # The system prompt must include the no-tools preamble (sourcemap parity).
    assert NO_TOOLS_PREAMBLE in call["system"]
    # The original text must appear in the user prompt.
    assert long_input in call["user"]


async def test_llm_failure_increments_breaker_and_falls_through_to_original() -> None:
    caller = _StubCaller(raise_exc=RuntimeError("provider 502"))
    c = Compactor(caller, policy=_policy())
    long_input = "y" * 500
    result = await c.compact(long_input)
    assert result.was_compacted is False
    assert result.compacted == long_input
    assert result.failure_reason is not None
    assert "llm_error" in result.failure_reason
    assert "RuntimeError" in result.failure_reason
    assert "provider 502" in result.failure_reason
    assert c.consecutive_failures == 1


async def test_three_consecutive_failures_trip_circuit_breaker() -> None:
    caller = _StubCaller(raise_exc=RuntimeError("nope"))
    c = Compactor(caller, policy=_policy())
    long_input = "z" * 500
    # Three failures bring the breaker to its threshold.
    for _ in range(3):
        r = await c.compact(long_input)
        assert r.was_compacted is False
    assert c.consecutive_failures == 3
    assert len(caller.calls) == 3
    # Fourth call must skip the LLM entirely (breaker tripped).
    r4 = await c.compact(long_input)
    assert r4.was_compacted is False
    assert r4.failure_reason == "circuit_breaker_tripped"
    assert len(caller.calls) == 3  # no new invocation


async def test_circuit_breaker_resets_on_success() -> None:
    # Two failures, then a success → breaker resets.
    summary = "ok" * 30  # 60 chars; above min(20), below 500
    caller = _StubCaller(responses=["short", "short", summary])
    # Use a policy where "short" (5 chars) is too short to count as success.
    c = Compactor(caller, policy=_policy(min_output_chars=20))
    long_input = "a" * 500
    r1 = await c.compact(long_input)
    r2 = await c.compact(long_input)
    assert r1.was_compacted is False and r2.was_compacted is False
    assert c.consecutive_failures == 2
    r3 = await c.compact(long_input)
    assert r3.was_compacted is True
    assert r3.compacted == summary
    assert c.consecutive_failures == 0


async def test_output_too_short_counts_as_failure() -> None:
    caller = _StubCaller(responses=["tiny"])  # 4 chars, below min(20)
    c = Compactor(caller, policy=_policy())
    result = await c.compact("y" * 500)
    assert result.was_compacted is False
    assert result.failure_reason == "output_too_short"
    assert c.consecutive_failures == 1


async def test_output_not_smaller_than_input_counts_as_failure() -> None:
    # Summary is the SAME size as the input — no compression achieved.
    long_input = "y" * 500
    same_size = "z" * 500
    caller = _StubCaller(responses=[same_size])
    c = Compactor(caller, policy=_policy())
    result = await c.compact(long_input)
    assert result.was_compacted is False
    assert result.failure_reason == "no_compression_achieved"
    assert result.compacted == long_input  # original kept
    assert c.consecutive_failures == 1


async def test_paper_compactor_uses_specialized_system_prompt() -> None:
    summary = "## Methods\n" + ("dense " * 30)  # ~190 chars
    caller = _StubCaller(responses=[summary])
    c = PaperCompactor(caller, policy=_policy())
    long_input = "raw paper " * 100  # 1000 chars
    result = await c.compact(long_input)
    assert result.was_compacted is True
    system = caller.calls[0]["system"]
    # No-tools preamble is always there.
    assert NO_TOOLS_PREAMBLE in system
    # Domain-specific markers from PaperCompactor.SYSTEM_PROMPT.
    assert "academic paper" in system
    assert "DOIs only" in system
    assert "Preserve the source language" in system


async def test_build_user_prompt_subclass_override() -> None:
    class _SubCompactor(Compactor):
        SYSTEM_PROMPT = "Tiny system."

        def build_user_prompt(self, text: str) -> str:
            return f"CUSTOM PREFIX :: {text}"

    summary = "compressed enough"  # 17 chars - below min(20) → too short
    caller = _StubCaller(responses=["x" * 40])  # OK shape
    c = _SubCompactor(caller, policy=_policy())
    await c.compact("y" * 500)
    user = caller.calls[0]["user"]
    assert user.startswith("CUSTOM PREFIX :: ")
    assert "y" * 500 in user
    # Sanity: the variable `summary` is unused on the green path; keep it
    # bound so a future ruff run with F841 doesn't trip. Reference it:
    assert isinstance(summary, str)


async def test_failure_reason_is_populated_correctly() -> None:
    # 1) below_threshold
    caller = _StubCaller(responses=["unused"])
    c = Compactor(caller, policy=_policy(threshold_chars=1_000))
    r = await c.compact("tiny")
    assert r.failure_reason == "below_threshold"

    # 2) llm_error: <ExcType>: <msg>
    caller_err = _StubCaller(raise_exc=ValueError("boom"))
    c_err = Compactor(caller_err, policy=_policy())
    r_err = await c_err.compact("z" * 500)
    assert r_err.failure_reason is not None
    assert r_err.failure_reason.startswith("llm_error: ValueError: boom")

    # 3) output_too_short
    caller_short = _StubCaller(responses=["t"])
    c_short = Compactor(caller_short, policy=_policy())
    r_short = await c_short.compact("z" * 500)
    assert r_short.failure_reason == "output_too_short"

    # 4) no_compression_achieved
    caller_same = _StubCaller(responses=["z" * 600])
    c_same = Compactor(caller_same, policy=_policy())
    r_same = await c_same.compact("y" * 500)
    assert r_same.failure_reason == "no_compression_achieved"

    # 5) circuit_breaker_tripped
    caller_break = _StubCaller(raise_exc=RuntimeError("x"))
    c_break = Compactor(caller_break, policy=_policy())
    for _ in range(3):
        await c_break.compact("z" * 500)
    r_break = await c_break.compact("z" * 500)
    assert r_break.failure_reason == "circuit_breaker_tripped"

    # 6) success → failure_reason is None
    caller_ok = _StubCaller(responses=["x" * 40])
    c_ok = Compactor(caller_ok, policy=_policy())
    r_ok = await c_ok.compact("z" * 500)
    assert r_ok.was_compacted is True
    assert r_ok.failure_reason is None


async def test_summary_is_stripped_before_length_checks() -> None:
    """LLM output with surrounding whitespace must be stripped first.

    Mirrors the existing `_compact_one` behavior in `searcher.py` that called
    `.strip()` on the LLM's text before returning it.
    """
    summary_padded = "   " + ("x" * 40) + "\n\n"
    caller = _StubCaller(responses=[summary_padded])
    c = Compactor(caller, policy=_policy())
    result = await c.compact("y" * 500)
    assert result.was_compacted is True
    assert result.compacted == "x" * 40  # stripped
    assert result.compacted_chars == 40


async def test_paper_compactor_default_policy_matches_searcher_threshold() -> None:
    """The default policy must keep the Searcher's 24k threshold envelope."""
    c = PaperCompactor(_StubCaller())
    assert c.policy.threshold_chars == 24_000
    assert c.policy.target_chars == 24_000
    assert c.policy.max_consecutive_failures == 3
