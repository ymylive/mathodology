"""CoderAgent smoke test — uses a real `KernelSession`, mocks the LLM.

We fake `GatewayClient.stream_completion` to return one turn that sets
`done=true` with trivial code. That exercises the full pipeline (LLM parse,
kernel execute, notebook write) without needing a real gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from agent_worker.agents import CoderAgent
from agent_worker.kernel import KernelSession
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    ModelSpec,
    ProblemInput,
)


class _FakeEmitter:
    """Record emit() calls; expose the same `run_id` attribute as EventEmitter."""

    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(
        self, kind: str, payload: dict | None = None, agent: str | None = None
    ) -> None:
        self.events.append((kind, payload or {}, agent))


class _FakeGateway:
    """Only `stream_completion` is used by the CoderAgent in these tests."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        for c in self._chunks:
            yield c

    async def close(self) -> None:
        pass


@pytest.fixture
def problem() -> ProblemInput:
    return ProblemInput(problem_text="Compute 1 + 2 and report the answer.")


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


async def test_coder_agent_runs_single_turn(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    directive_json = (
        '{"reasoning":"just compute","code":"print(1+2)",'
        '"done":true,"summary":"The answer is 3."}'
    )
    gateway = _FakeGateway([directive_json])
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    output = await agent.run(problem, analysis, spec)

    assert len(output.cells) == 1
    assert output.cells[0].index == 0
    assert "3" in output.cells[0].stdout
    assert output.cells[0].error is None
    assert output.final_summary == "The answer is 3."
    assert Path(output.notebook_path).is_file()  # noqa: ASYNC240 — stdlib asyncio test

    kinds = [e[0] for e in emitter.events]
    assert "stage.start" in kinds
    assert "agent.output" in kinds
    assert "stage.done" in kinds


async def test_coder_agent_emits_kernel_stdout(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    directive_json = (
        '{"reasoning":"print","code":"print(\\"hello from coder\\")",'
        '"done":true,"summary":"printed"}'
    )
    gateway = _FakeGateway([directive_json])
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    await agent.run(problem, analysis, spec)

    stdout_events = [e for e in emitter.events if e[0] == "kernel.stdout"]
    assert stdout_events, "expected at least one kernel.stdout event"
    joined = "".join(e[1].get("text", "") for e in stdout_events)
    assert "hello from coder" in joined


async def test_coder_agent_retries_on_parse_error(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    # First response is unparseable; agent does NOT auto-retry because the
    # fake gateway is constructed with a single chunk set. So we drive the
    # retry branch by returning garbage on the first stream_completion call
    # and a valid directive on the second.
    class _RetryingGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_completion(self, **_: object) -> AsyncIterator[str]:
            self.calls += 1
            if self.calls == 1:
                yield "definitely not json"
            else:
                yield (
                    '{"reasoning":"retry","code":"x=5","done":true,'
                    '"summary":"retried"}'
                )

    gateway = _RetryingGateway()
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    output = await agent.run(problem, analysis, spec)

    assert gateway.calls == 2
    assert output.final_summary == "retried"
    assert len(output.cells) == 1


async def test_coder_agent_parses_lenient_multi_object(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    """Round-10 regression: gpt-5.5 with reasoning_effort=high sometimes
    emits one valid CoderDirective JSON followed by a second one (or
    free-form prose) back-to-back without an array wrapper. Strict
    `orjson.loads` rejects the whole blob and the QA run failed at Coder
    after 3 stream retries (¥1.25 wasted). The parser now falls back to
    `json.JSONDecoder.raw_decode` and accepts the first valid object.
    """

    class _MultiObjGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_completion(self, **_: object) -> AsyncIterator[str]:
            self.calls += 1
            # Two back-to-back JSON objects; first is the only one the
            # parser should consume. Without lenient parse this raises
            # `AgentParseError: not valid JSON: unexpected end of data`.
            yield (
                '{"reasoning":"first object","code":"print(7)","done":true,'
                '"summary":"first done"}'
                '{"reasoning":"second object","code":"x=1","done":false,'
                '"summary":null}'
            )

        async def close(self) -> None:
            pass

    gateway = _MultiObjGateway()
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    output = await agent.run(problem, analysis, spec)

    # Parse succeeded on the first try — no retry needed.
    assert gateway.calls == 1
    assert output.final_summary == "first done"
    assert len(output.cells) == 1


async def test_coder_agent_respects_explicit_iteration_cap(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    class _LoopingGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_completion(self, **_: object) -> AsyncIterator[str]:
            self.calls += 1
            yield (
                '{"reasoning":"continue","code":"print(\\"cell '
                f'{self.calls}\\")","done":false,"summary":null}}'
            )

    gateway = _LoopingGateway()
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    output = await agent.run(problem, analysis, spec, max_iterations=2)

    assert gateway.calls == 2
    assert len(output.cells) == 2
    assert output.final_summary == "Reached iteration limit without explicit done."
