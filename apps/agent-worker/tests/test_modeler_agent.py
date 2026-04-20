"""ModelerAgent smoke test — fakes the LLM to yield a ModelSpec JSON."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from agent_worker.agents import ModelerAgent
from mm_contracts import AnalyzerOutput, ApproachSketch, ModelSpec, ProblemInput


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
        problem_text="Model a simple queue and compute steady-state length.",
        competition_type="mcm",
    )


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Model a single-server queue and report its mean length.",
        sub_questions=["What is the stationary distribution?"],
        proposed_approaches=[
            ApproachSketch(name="M/M/1", rationale="canonical", methods=["analytic"]),
        ],
    )


async def test_modeler_agent_returns_validated_spec(
    problem: ProblemInput, analysis: AnalyzerOutput
) -> None:
    spec_json = (
        '{"chosen_approach":"M/M/1 queue",'
        '"rationale":"Poisson arrivals + exponential service fit the problem.",'
        '"variables":['
        '{"symbol":"\\\\lambda","name":"arrival rate","unit":"1/s","description":"mean arrivals per second"},'
        '{"symbol":"\\\\mu","name":"service rate","unit":"1/s","description":"mean services per second"}'
        "],"
        '"equations":['
        '{"latex":"L = \\\\rho / (1 - \\\\rho)","description":"mean queue length where rho = lambda/mu"}'
        "],"
        '"algorithm_outline":["Pick lambda, mu","Verify rho<1","Compute L"],'
        '"complexity_notes":"O(1) closed form",'
        '"validation_strategy":"Monte-Carlo simulation vs analytic L"}'
    )
    gateway = _FakeGateway([spec_json])
    emitter = _FakeEmitter()

    agent = ModelerAgent(gateway, emitter)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, ModelSpec)
    assert out.chosen_approach == "M/M/1 queue"
    assert len(out.variables) == 2
    assert out.variables[0].symbol == "\\lambda"
    assert len(out.equations) == 1
    assert out.equations[0].latex.startswith("L = ")
    assert out.algorithm_outline  # min_length=1 guaranteed by contract
    assert out.validation_strategy

    kinds = [e[0] for e in emitter.events]
    assert "stage.start" in kinds
    assert "agent.output" in kinds
    assert "stage.done" in kinds
