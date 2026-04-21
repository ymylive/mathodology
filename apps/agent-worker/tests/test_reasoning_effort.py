"""Verify every agent threads run-level `reasoning_effort` into the gateway.

We mock `GatewayClient.stream_completion` with a recording fake that captures
every call's keyword arguments, then construct each agent with
`run_effort="high"` and assert the mock receives `reasoning_effort="high"`.
Each agent gets a minimal valid response so its own parse/output path
terminates successfully.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from agent_worker.agents import (
    AnalyzerAgent,
    CoderAgent,
    ModelerAgent,
    SearcherAgent,
    WriterAgent,
)
from agent_worker.kernel import KernelSession
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    CellExecution,
    CoderOutput,
    ModelSpec,
    Paper,
    ProblemInput,
    SearchFindings,
)


class _RecordingEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(
        self, kind: str, payload: dict | None = None, agent: str | None = None
    ) -> None:
        self.events.append((kind, payload or {}, agent))


class _RecordingGateway:
    """Fake gateway that captures `stream_completion` kwargs and replays chunks."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.calls: list[dict] = []

    async def stream_completion(self, **kwargs: object) -> AsyncIterator[str]:
        self.calls.append(dict(kwargs))
        for c in self._chunks:
            yield c

    async def close(self) -> None:  # pragma: no cover — never called in-test
        pass


_ANALYZER_JSON = (
    '{"restated_problem":"A 20-char problem.",'
    '"sub_questions":["q"],'
    '"proposed_approaches":[{"name":"n","rationale":"r","methods":[]}]}'
)
_MODELER_JSON = (
    '{"chosen_approach":"direct",'
    '"rationale":"trivial",'
    '"algorithm_outline":["x"],'
    '"validation_strategy":"eyeball"}'
)
_CODER_DIRECTIVE = (
    '{"reasoning":"just compute","code":"print(1+2)",'
    '"done":true,"summary":"The answer is 3."}'
)
_SEARCHER_JSON = (
    '{"queries":["q"],"papers":[],"key_findings":[],"datasets_mentioned":[]}'
)
_WRITER_JSON = (
    '{"title":"t","abstract":"a",'
    '"sections":[{"title":"s","body_markdown":"b"}],'
    '"references":[],"figure_refs":[]}'
)


@pytest.fixture
def problem() -> ProblemInput:
    return ProblemInput(
        problem_text="Compute 1 + 2 and report the answer.",
        reasoning_effort="high",
    )


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Compute 1+2 and report.",
        sub_questions=["What is 1+2?"],
        proposed_approaches=[
            ApproachSketch(name="direct", rationale="trivial", methods=["arithmetic"]),
        ],
    )


@pytest.fixture
def spec() -> ModelSpec:
    return ModelSpec(
        chosen_approach="direct arithmetic",
        rationale="trivial problem",
        algorithm_outline=["compute 1+2", "print result"],
        validation_strategy="eyeball",
    )


@pytest.fixture
def coder_output() -> CoderOutput:
    return CoderOutput(
        cells=[
            CellExecution(index=0, source="print(1+2)", stdout="3\n", result_text=None)
        ],
        figure_paths=[],
        final_summary="done",
        notebook_path="/tmp/nb.ipynb",
    )


async def test_analyzer_forwards_run_effort(problem: ProblemInput) -> None:
    gateway = _RecordingGateway([_ANALYZER_JSON])
    emitter = _RecordingEmitter()
    agent = AnalyzerAgent(gateway, emitter, run_effort="high")  # type: ignore[arg-type]
    await agent.run_for_problem(problem)
    assert gateway.calls, "expected stream_completion to be called"
    assert gateway.calls[0].get("reasoning_effort") == "high"


async def test_modeler_forwards_run_effort(
    problem: ProblemInput, analysis: AnalyzerOutput
) -> None:
    gateway = _RecordingGateway([_MODELER_JSON])
    emitter = _RecordingEmitter()
    agent = ModelerAgent(gateway, emitter, run_effort="high")  # type: ignore[arg-type]
    await agent.run_for(problem, analysis)
    assert gateway.calls[0].get("reasoning_effort") == "high"


async def test_searcher_forwards_run_effort(
    problem: ProblemInput, analysis: AnalyzerOutput, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force synthesis path by returning one paper from the arXiv stub.
    async def _stub_batch_search_arxiv(
        queries: list[str], max_per_query: int = 5, concurrency: int = 2
    ) -> dict[str, list[Paper]]:
        return {
            queries[0]: [
                Paper(
                    title="Traffic Signal Optimization",
                    url="https://arxiv.org/abs/0000.00001",
                    arxiv_id="0000.00001",
                )
            ]
        }

    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_arxiv",
        _stub_batch_search_arxiv,
    )

    gateway = _RecordingGateway([_SEARCHER_JSON])
    emitter = _RecordingEmitter()
    agent = SearcherAgent(gateway, emitter, run_effort="high")  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)
    assert isinstance(out, SearchFindings)
    assert gateway.calls, "synthesis must call the LLM with the paper list"
    assert gateway.calls[0].get("reasoning_effort") == "high"


async def test_coder_forwards_run_effort(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    gateway = _RecordingGateway([_CODER_DIRECTIVE])
    emitter = _RecordingEmitter()
    kernel = KernelSession(uuid4(), tmp_path)
    agent = CoderAgent(gateway, emitter, kernel, run_effort="high")  # type: ignore[arg-type]
    await agent.run(problem, analysis, spec)
    assert gateway.calls, "coder must call the LLM at least once"
    assert gateway.calls[0].get("reasoning_effort") == "high"


async def test_writer_forwards_run_effort(
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_output: CoderOutput,
) -> None:
    gateway = _RecordingGateway([_WRITER_JSON])
    emitter = _RecordingEmitter()
    agent = WriterAgent(gateway, emitter, run_effort="high")  # type: ignore[arg-type]
    await agent.run_for(problem, analysis, spec, coder_output)
    assert gateway.calls[0].get("reasoning_effort") == "high"
