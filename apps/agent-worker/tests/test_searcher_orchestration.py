"""Orchestration tests for the Searcher agent under different SearchConfig routes.

Mocks out `batch_search_arxiv`, `batch_search_tavily`, and `batch_search_web`
at module-level so the subprocess / network is never touched. Each test sets
a different `SearchConfig` / env state and asserts which tools were called
and which were skipped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agent_worker.agents import SearcherAgent
from agent_worker.config import Settings
from agent_worker.tools.tavily import TavilyResult
from agent_worker.tools.web_search_mcp import WebResult
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    Paper,
    ProblemInput,
    SearchConfig,
    SearchFindings,
)


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(
        self, kind: str, payload: dict | None = None, agent: str | None = None
    ) -> None:
        self.events.append((kind, payload or {}, agent))

    def log_messages(self) -> list[str]:
        return [
            e[1].get("message", "") for e in self.events if e[0] == "log"
        ]


class _NullGateway:
    """Gateway that yields a canned SearchFindings JSON blob."""

    def __init__(self, n_papers: int = 1) -> None:
        self._n = n_papers

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        # Minimal valid SearchFindings — the orchestration tests don't care
        # about the synthesized content, only which tools ran.
        yield (
            '{"queries":["q1"],'
            '"papers":[],'
            '"key_findings":[],'
            '"datasets_mentioned":[]}'
        )

    async def close(self) -> None:
        pass


class _ExplodingGateway:
    """Used when we expect the LLM synth path to be skipped."""

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        raise AssertionError("LLM synth should not be called")
        yield ""

    async def close(self) -> None:
        pass


@pytest.fixture
def problem_base() -> ProblemInput:
    return ProblemInput(
        problem_text="Model traffic flow in a city grid.",
        competition_type="mcm",
    )


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Forecast traffic density and recommend signal timing.",
        sub_questions=["How does signal timing affect throughput?"],
        proposed_approaches=[
            ApproachSketch(
                name="queueing network",
                rationale="links are queues",
                methods=["M/M/c"],
            ),
        ],
    )


def _one_arxiv_paper() -> list[Paper]:
    return [
        Paper(
            title="Adaptive Signal Timing",
            authors=["X. Smith"],
            abstract="abs",
            url="http://arxiv.org/abs/2301.00001",
            arxiv_id="2301.00001",
            published="2023-01-01",
        )
    ]


def _tavily_hit(url: str = "https://example.com/a") -> TavilyResult:
    return TavilyResult(
        title="Example",
        url=url,
        content="snippet",
        score=0.9,
        published_date="2024-01-01",
        query="q",
    )


def _web_hit(url: str = "https://web.example/x") -> WebResult:
    return WebResult(
        title="Web",
        url=url,
        description="snippet",
        engine="baidu",
        source="web.example",
        query="q",
    )


# --- Helpers ---------------------------------------------------------------


class _Stubs:
    """Container for per-test mock state + a `wire` helper."""

    def __init__(self) -> None:
        self.arxiv_calls = 0
        self.tavily_calls = 0
        self.web_calls = 0
        self.arxiv_return: dict[str, list[Paper]] = {}
        self.tavily_return: dict[str, list[TavilyResult]] = {}
        self.web_return: dict[str, list[WebResult]] = {}

    def wire(self, monkeypatch: pytest.MonkeyPatch, *, tavily_api_key: str = "") -> None:
        async def fake_arxiv(queries, max_per_query=5, concurrency=2):  # noqa: ANN001
            self.arxiv_calls += 1
            return {q: self.arxiv_return.get(q, []) for q in queries}

        async def fake_tavily(queries, api_key, **_):  # noqa: ANN001, ANN003
            self.tavily_calls += 1
            return {q: self.tavily_return.get(q, []) for q in queries}

        async def fake_web(queries, **_):  # noqa: ANN001, ANN003
            self.web_calls += 1
            return {q: self.web_return.get(q, []) for q in queries}

        monkeypatch.setattr(
            "agent_worker.agents.searcher.batch_search_arxiv", fake_arxiv
        )
        monkeypatch.setattr(
            "agent_worker.agents.searcher.batch_search_tavily", fake_tavily
        )
        monkeypatch.setattr(
            "agent_worker.agents.searcher.batch_search_web", fake_web
        )

        def _fake_settings() -> Settings:
            # Build with direct field defaults then override TAVILY_API_KEY.
            return Settings(TAVILY_API_KEY=tavily_api_key)

        monkeypatch.setattr(
            "agent_worker.agents.searcher.get_settings", _fake_settings
        )


# --- Tests -----------------------------------------------------------------


async def test_primary_tavily_no_fallback_when_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    stubs = _Stubs()
    stubs.arxiv_return = {"queueing network": _one_arxiv_paper()}
    # 5 unique tavily hits >= threshold=3 → no fallback.
    stubs.tavily_return = {
        "queueing network": [
            _tavily_hit(f"https://example.com/a{i}") for i in range(5)
        ]
    }
    stubs.wire(monkeypatch, tavily_api_key="k")

    problem = problem_base.model_copy(
        update={
            "search_config": SearchConfig(
                primary="tavily", fallback_threshold=3
            )
        }
    )
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, SearchFindings)
    assert stubs.arxiv_calls == 1
    assert stubs.tavily_calls == 1
    assert stubs.web_calls == 0, "fallback should not have fired"
    msgs = emitter.log_messages()
    assert any("primary=tavily" in m for m in msgs)
    assert not any("fallback triggered" in m for m in msgs)


async def test_primary_tavily_falls_back_when_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    stubs = _Stubs()
    stubs.arxiv_return = {}
    # Only 1 tavily hit < threshold=3 → open-webSearch fallback.
    stubs.tavily_return = {
        "queueing network": [_tavily_hit("https://example.com/only")]
    }
    stubs.web_return = {
        "queueing network": [_web_hit("https://web.example/fallback")]
    }
    stubs.wire(monkeypatch, tavily_api_key="k")

    problem = problem_base.model_copy(
        update={
            "search_config": SearchConfig(
                primary="tavily", fallback_threshold=3
            )
        }
    )
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)
    assert isinstance(out, SearchFindings)
    assert stubs.tavily_calls == 1
    assert stubs.web_calls == 1, "fallback should have fired"
    msgs = emitter.log_messages()
    assert any("fallback triggered" in m for m in msgs)


async def test_primary_tavily_auto_demotes_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    stubs = _Stubs()
    stubs.arxiv_return = {}
    stubs.web_return = {
        "queueing network": [_web_hit()]
    }
    # No tavily_api_key in settings → primary=tavily silently becomes
    # open_websearch for this run.
    stubs.wire(monkeypatch, tavily_api_key="")

    problem = problem_base.model_copy(
        update={"search_config": SearchConfig(primary="tavily")}
    )
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)

    assert stubs.tavily_calls == 0, "tavily must be skipped when key is unset"
    assert stubs.web_calls == 1
    msgs = emitter.log_messages()
    assert any("primary=tavily skipped" in m for m in msgs)


async def test_primary_open_websearch_skips_tavily(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    stubs = _Stubs()
    stubs.arxiv_return = {}
    stubs.web_return = {"queueing network": [_web_hit()]}
    # Even WITH a tavily key, primary=open_websearch must not call Tavily.
    stubs.wire(monkeypatch, tavily_api_key="k")

    problem = problem_base.model_copy(
        update={"search_config": SearchConfig(primary="open_websearch")}
    )
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)

    assert stubs.tavily_calls == 0
    assert stubs.web_calls == 1
    msgs = emitter.log_messages()
    assert any("primary=open_websearch" in m for m in msgs)


async def test_primary_open_websearch_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    """Even when open_websearch returns 0, no fallback is triggered."""
    stubs = _Stubs()
    stubs.arxiv_return = {}
    stubs.web_return = {}
    stubs.wire(monkeypatch, tavily_api_key="k")

    problem = problem_base.model_copy(
        update={
            "search_config": SearchConfig(
                primary="open_websearch", fallback_threshold=10
            )
        }
    )
    emitter = _FakeEmitter()
    # arXiv also empty → LLM synth is skipped (emits empty SearchFindings),
    # so use an exploding gateway to assert that explicitly.
    agent = SearcherAgent(_ExplodingGateway(), emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, SearchFindings)
    assert stubs.tavily_calls == 0
    assert stubs.web_calls == 1
    msgs = emitter.log_messages()
    assert not any("fallback triggered" in m for m in msgs)


async def test_primary_none_skips_all_web_sources(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    stubs = _Stubs()
    stubs.arxiv_return = {"queueing network": _one_arxiv_paper()}
    stubs.wire(monkeypatch, tavily_api_key="k")

    problem = problem_base.model_copy(
        update={"search_config": SearchConfig(primary="none")}
    )
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)

    assert stubs.arxiv_calls == 1
    assert stubs.tavily_calls == 0
    assert stubs.web_calls == 0


async def test_tavily_fallback_merges_cross_source_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    """If Tavily and the fallback web search both surface the same URL,
    the merged `unique` list contains exactly one entry for it."""
    shared_url = "https://example.com/same"
    stubs = _Stubs()
    stubs.arxiv_return = {}
    stubs.tavily_return = {
        "queueing network": [_tavily_hit(shared_url)]
    }
    # Fallback web returns the SAME url → expect dedupe.
    stubs.web_return = {
        "queueing network": [_web_hit(shared_url)]
    }
    stubs.wire(monkeypatch, tavily_api_key="k")

    problem = problem_base.model_copy(
        update={
            "search_config": SearchConfig(
                primary="tavily", fallback_threshold=10
            )
        }
    )
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)

    msgs = emitter.log_messages()
    # The retrieved-unique-papers log records the total; after dedupe it
    # must still be 1 (tavily kept the URL; web's duplicate dropped).
    summary = next(m for m in msgs if "unique papers" in m)
    assert "tavily=1" in summary
    assert "web=0" in summary


async def test_no_search_config_uses_env_default(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    """ProblemInput.search_config=None → worker picks tavily when env has key."""
    stubs = _Stubs()
    stubs.arxiv_return = {}
    stubs.tavily_return = {
        "queueing network": [
            _tavily_hit(f"https://example.com/{i}") for i in range(5)
        ]
    }
    stubs.wire(monkeypatch, tavily_api_key="env-key")

    # No search_config on the ProblemInput.
    problem = problem_base.model_copy(update={"search_config": None})
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)

    assert stubs.tavily_calls == 1


async def test_no_search_config_uses_env_default_without_key(
    monkeypatch: pytest.MonkeyPatch,
    problem_base: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    """Env without TAVILY_API_KEY → default primary is open_websearch."""
    stubs = _Stubs()
    stubs.arxiv_return = {}
    stubs.web_return = {"queueing network": [_web_hit()]}
    stubs.wire(monkeypatch, tavily_api_key="")

    problem = problem_base.model_copy(update={"search_config": None})
    emitter = _FakeEmitter()
    agent = SearcherAgent(_NullGateway(), emitter)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)

    assert stubs.tavily_calls == 0
    assert stubs.web_calls == 1
