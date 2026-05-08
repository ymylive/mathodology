"""Searcher.refine_queries — bibliographic rewrite + strict fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agent_worker.agents import SearcherAgent
from mm_contracts import ProblemInput


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(
        self, kind: str, payload: dict | None = None, agent: str | None = None
    ) -> None:
        self.events.append((kind, payload or {}, agent))


class _ScriptedGateway:
    """Yields a single string in chunks to mimic SSE streaming."""

    def __init__(self, response: str | Exception) -> None:
        self._response = response

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        if isinstance(self._response, Exception):
            raise self._response
        yield self._response

    async def close(self) -> None:
        pass


def _make_agent(
    response: str | Exception, emitter: _FakeEmitter | None = None
) -> tuple[SearcherAgent, _FakeEmitter]:
    em = emitter or _FakeEmitter()
    agent = SearcherAgent(_ScriptedGateway(response), em)  # type: ignore[arg-type]
    return agent, em


# --- Happy path ---


async def test_refine_queries_returns_llm_output_for_english_problem() -> None:
    agent, _ = _make_agent(
        '{"queries":['
        '"M/M/c queue bank branch waiting time",'
        '"Erlang C bank staffing",'
        '"queueing theory service capacity",'
        '"multi-server queue stationary distribution"]}'
    )
    problem = ProblemInput(problem_text="Bank branch queueing analysis.")
    out = await agent._refine_queries(
        problem, ["raw1", "raw2", "raw3"]
    )
    assert out == [
        "M/M/c queue bank branch waiting time",
        "Erlang C bank staffing",
        "queueing theory service capacity",
        "multi-server queue stationary distribution",
    ]


async def test_refine_queries_keeps_chinese_query_for_cjk_problem() -> None:
    agent, _ = _make_agent(
        '{"queries":['
        '"M/M/c queue bank waiting time",'
        '"Erlang C staffing model",'
        '"multi-server queueing theory",'
        '"银行 网点 排队论 等候时间"]}'
    )
    problem = ProblemInput(
        problem_text="请用排队论分析银行网点高峰期等候时间。",
        competition_type="cumcm",
    )
    out = await agent._refine_queries(problem, ["raw1", "raw2"])
    assert len(out) == 4
    # The Chinese query should be preserved.
    assert any("银行" in q for q in out)


async def test_refine_queries_dedupes_and_caps_at_six() -> None:
    duplicates = '","'.join(["queue model"] * 8)
    agent, _ = _make_agent(f'{{"queries":["{duplicates}"]}}')
    problem = ProblemInput(problem_text="anything")
    out = await agent._refine_queries(problem, ["raw"])
    assert out == ["queue model"]  # full dedupe → one item


# --- Fallback paths (refinement must never make Searcher fail) ---


async def test_refine_queries_falls_back_when_llm_raises() -> None:
    agent, em = _make_agent(RuntimeError("provider 502"))
    problem = ProblemInput(problem_text="x")
    raw = ["raw a", "raw b"]
    out = await agent._refine_queries(problem, raw)
    assert out == raw
    warns = [e for e in em.events if e[0] == "log" and e[1].get("level") == "warning"]
    assert any("query refinement failed" in e[1]["message"] for e in warns)


async def test_refine_queries_falls_back_on_invalid_json() -> None:
    agent, em = _make_agent("not json at all")
    out = await agent._refine_queries(
        ProblemInput(problem_text="x"), ["raw a"]
    )
    assert out == ["raw a"]
    assert any(
        "query refinement failed" in e[1].get("message", "")
        for e in em.events
        if e[0] == "log"
    )


async def test_refine_queries_falls_back_on_empty_array() -> None:
    agent, _ = _make_agent('{"queries":[]}')
    out = await agent._refine_queries(
        ProblemInput(problem_text="x"), ["raw"]
    )
    assert out == ["raw"]


async def test_refine_queries_falls_back_when_queries_field_missing() -> None:
    agent, _ = _make_agent('{"something_else":["a","b"]}')
    out = await agent._refine_queries(
        ProblemInput(problem_text="x"), ["raw1", "raw2"]
    )
    assert out == ["raw1", "raw2"]


async def test_refine_queries_returns_unchanged_for_empty_input() -> None:
    """No raw queries → no LLM call (and no fallback log)."""
    agent, em = _make_agent('{"queries":["should-not-appear"]}')
    out = await agent._refine_queries(
        ProblemInput(problem_text="x"), []
    )
    assert out == []
    # No LLM was invoked, so no fallback warning either.
    assert not any(
        "refinement" in e[1].get("message", "")
        for e in em.events
        if e[0] == "log"
    )


# --- Sanity: refinement skips bogus entries instead of crashing ---


async def test_refine_queries_filters_non_string_entries() -> None:
    agent, _ = _make_agent(
        '{"queries":["good query", null, 42, "", "  ", "another good"]}'
    )
    out = await agent._refine_queries(
        ProblemInput(problem_text="x"), ["raw"]
    )
    assert out == ["good query", "another good"]


@pytest.mark.parametrize(
    "raw_input,expected",
    [
        (["only one"], None),  # exercises happy path with single raw input
        (["a", "b", "c", "d", "e", "f", "g"], None),  # >5 still fine
    ],
)
async def test_refine_queries_handles_varied_raw_input_sizes(
    raw_input: list[str], expected: list[str] | None
) -> None:
    agent, _ = _make_agent('{"queries":["x"]}')
    out = await agent._refine_queries(ProblemInput(problem_text="x"), raw_input)
    assert out == ["x"]
    _ = expected  # unused; parametrize signal only
