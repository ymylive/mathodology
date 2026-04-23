"""SearcherAgent tests — mock the arXiv tool and the LLM gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agent_worker.agents import SearcherAgent
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    DataRequirement,
    Paper,
    ProblemInput,
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


class _FakeGateway:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        for c in self._chunks:
            yield c

    async def close(self) -> None:
        pass


@pytest.fixture
def problem() -> ProblemInput:
    return ProblemInput(
        problem_text="Model traffic flow in a city grid.",
        competition_type="mcm",
    )


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Forecast traffic density and recommend signal timing.",
        sub_questions=[
            "How does signal timing affect throughput?",
            "Where are the bottlenecks?",
            "What inputs predict congestion?",
        ],
        data_requirements=[
            DataRequirement(name="traffic count data", description="hourly counts"),
            DataRequirement(name="road network graph", description="OSM extract"),
        ],
        proposed_approaches=[
            ApproachSketch(
                name="queueing network",
                rationale="links are queues",
                methods=["M/M/c", "max-flow"],
            ),
        ],
    )


def _sample_papers() -> list[Paper]:
    return [
        Paper(
            title="Adaptive Signal Timing",
            authors=["X. Smith"],
            abstract="We propose an RL-based adaptive traffic signal controller.",
            url="http://arxiv.org/abs/2301.00001",
            arxiv_id="2301.00001",
            published="2023-01-01",
        ),
        Paper(
            title="Queueing Analysis of Road Networks",
            authors=["Y. Zhang"],
            abstract="We apply M/M/c queue models to urban corridors.",
            url="http://arxiv.org/abs/2205.12345",
            arxiv_id="2205.12345",
            published="2022-05-20",
        ),
    ]


_FINDINGS_JSON = (
    '{"queries":["How does signal timing affect throughput?",'
    '"Where are the bottlenecks?","What inputs predict congestion?",'
    '"traffic count data","road network graph"],'
    '"papers":[{'
    '"title":"Adaptive Signal Timing",'
    '"authors":["X. Smith"],'
    '"abstract":"We propose an RL-based adaptive traffic signal controller.",'
    '"url":"http://arxiv.org/abs/2301.00001",'
    '"arxiv_id":"2301.00001","published":"2023-01-01",'
    '"relevance_reason":"Directly addresses the signal-timing sub-question."'
    "}],"
    '"key_findings":["RL-based controllers outperform fixed timing in simulation."],'
    '"datasets_mentioned":["PeMS"]}'
)


async def test_searcher_emits_expected_events(
    monkeypatch: pytest.MonkeyPatch,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    papers = _sample_papers()

    async def fake_batch(queries, max_per_query=5, concurrency=2):  # noqa: ANN001
        return {q: papers for q in queries[:1]} | {q: [] for q in queries[1:]}

    async def fake_web(queries, **_):  # noqa: ANN001, ANN003
        return {q: [] for q in queries}

    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_arxiv", fake_batch
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_web", fake_web
    )

    gateway = _FakeGateway([_FINDINGS_JSON])
    emitter = _FakeEmitter()

    agent = SearcherAgent(gateway, emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, SearchFindings)
    assert len(out.papers) == 1
    assert out.papers[0].arxiv_id == "2301.00001"
    assert out.key_findings

    kinds = [e[0] for e in emitter.events]
    # Expected order (post-Tavily refactor): stage.start, log(queries),
    # log(arXiv returned N), log(primary=… or arXiv-only), log(unique total),
    # agent.output, stage.done. The primary-source log only fires when the
    # web leg actually ran; in this test open-webSearch is stubbed to return
    # empty so primary=open_websearch is the effective selection (no
    # TAVILY_API_KEY in env).
    assert kinds[0] == "stage.start"
    assert kinds[1] == "log"
    assert "arXiv queries" in emitter.events[1][1]["message"]
    # Per-source logs, then unique-total. We scan instead of using hard
    # indices because the number of per-source lines depends on routing.
    log_messages = [
        e[1].get("message", "") for e in emitter.events if e[0] == "log"
    ]
    assert any("arXiv returned" in m for m in log_messages)
    assert any("unique papers" in m for m in log_messages)
    assert kinds[-2] == "agent.output"
    assert emitter.events[-2][1]["schema_name"] == "SearchFindings"
    assert kinds[-1] == "stage.done"


async def test_searcher_degrades_when_arxiv_empty(
    monkeypatch: pytest.MonkeyPatch,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    async def fake_batch_empty(queries, max_per_query=5, concurrency=2):  # noqa: ANN001
        return {q: [] for q in queries}

    async def fake_web_empty(queries, **_):  # noqa: ANN001, ANN003
        return {q: [] for q in queries}

    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_arxiv", fake_batch_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_web", fake_web_empty
    )

    # Gateway should never be called — assert that by failing the stream.
    class _ExplodingGateway:
        async def stream_completion(self, **_: object) -> AsyncIterator[str]:
            raise AssertionError("LLM must not be called when arXiv returned zero")
            yield ""  # make it an async generator syntactically

        async def close(self) -> None:
            pass

    emitter = _FakeEmitter()
    agent = SearcherAgent(_ExplodingGateway(), emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, SearchFindings)
    assert out.papers == []
    assert out.queries  # queries recorded even though no papers
    assert out.key_findings == []

    # Look for the "emitting empty SearchFindings" warning log.
    logs = [e for e in emitter.events if e[0] == "log"]
    assert any(
        "emitting empty SearchFindings" in e[1].get("message", "") for e in logs
    )
    kinds = [e[0] for e in emitter.events]
    assert kinds[-2] == "agent.output"
    assert kinds[-1] == "stage.done"


async def test_searcher_degrades_when_arxiv_raises(
    monkeypatch: pytest.MonkeyPatch,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
) -> None:
    async def fake_batch_raises(queries, max_per_query=5, concurrency=2):  # noqa: ANN001
        raise RuntimeError("network down")

    async def fake_web_empty(queries, **_):  # noqa: ANN001, ANN003
        return {q: [] for q in queries}

    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_arxiv", fake_batch_raises
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_web", fake_web_empty
    )

    class _NoopGateway:
        async def stream_completion(self, **_: object) -> AsyncIterator[str]:
            raise AssertionError("LLM must not be called")
            yield ""

        async def close(self) -> None:
            pass

    emitter = _FakeEmitter()
    agent = SearcherAgent(_NoopGateway(), emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, SearchFindings)
    assert out.papers == []
    warnings = [
        e
        for e in emitter.events
        if e[0] == "log" and e[1].get("level") == "warning"
    ]
    assert any("arXiv batch failed" in e[1]["message"] for e in warnings)


def test_build_queries_prioritizes_methodology(
    problem: ProblemInput, analysis: AnalyzerOutput
) -> None:
    """Methodology terms (approach name + methods) are the best arXiv hits;
    sub-questions come second. Data-requirement filenames are a last resort."""
    agent = SearcherAgent.__new__(SearcherAgent)
    qs = agent._build_queries(problem, analysis)
    assert 1 <= len(qs) <= 5
    # Broad coverage of what the Analyzer surfaced.
    assert any(kw in q.lower() for q in qs for kw in ("queueing", "signal", "throughput"))


def test_build_queries_skips_api_path_methods() -> None:
    """Method strings like 'numpy.polyfit' are poor arXiv queries — skipped."""
    agent = SearcherAgent.__new__(SearcherAgent)
    problem = ProblemInput(problem_text="fit a line")
    analysis = AnalyzerOutput(
        restated_problem="R" * 10,
        sub_questions=["sub q"],
        proposed_approaches=[
            ApproachSketch(
                name="Ordinary Least Squares",
                rationale="y",
                methods=["numpy.polyfit", "scipy.stats.linregress"],
            ),
        ],
    )
    qs = agent._build_queries(problem, analysis)
    assert "Ordinary Least Squares" in qs
    assert all(not q.startswith(("numpy.", "scipy.", "sklearn.")) for q in qs)


def test_build_queries_falls_back_to_problem_text() -> None:
    agent = SearcherAgent.__new__(SearcherAgent)
    problem = ProblemInput(problem_text="A very long problem statement." * 20)

    # When Analyzer has no usable signals at all, use problem text.
    empty_analysis = AnalyzerOutput(
        restated_problem="R" * 10,
        sub_questions=[""],  # empty string entry
        proposed_approaches=[
            ApproachSketch(name="", rationale="y", methods=[]),
        ],
    )
    qs = agent._build_queries(problem, empty_analysis)
    assert len(qs) == 1
    assert qs[0].startswith("A very long problem statement.")
