"""Tests for ModelerAgent + HMMLService integration.

Uses an in-memory HMMLService with two fake methods and a fake gateway that
returns a ModelSpec JSON already carrying `consulted_methods`. Verifies:
- HMML retrieval fires and a `log` event mentions the retrieved method names.
- The rendered user prompt contains the retrieved methods block.
- The returned ModelSpec preserves consulted_methods end-to-end.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from agent_worker.agents import ModelerAgent
from agent_worker.hmml import HMMLService
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    MethodNode,
    ModelSpec,
    ProblemInput,
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
    """Capture the exact messages the agent sent to the LLM."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.last_messages: list[dict[str, Any]] | None = None

    async def stream_completion(
        self, *, messages: list[dict[str, Any]], **_: object
    ) -> AsyncIterator[str]:
        self.last_messages = messages
        for c in self._chunks:
            yield c

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_hmml() -> HMMLService:
    return HMMLService(
        methods=[
            MethodNode(
                id="fake_mmc",
                name="Fake M/M/c Queue",
                domain="simulation_queueing",
                subdomain="queueing_theory",
                applicable_scenarios=[
                    "multi-server Poisson arrival Markov queue",
                    "staffing decisions",
                ],
                math_form="rho = lambda / (c mu)",
                python_template="# placeholder",
                keywords=["queue", "queueing", "M/M/c", "排队"],
            ),
            MethodNode(
                id="fake_linreg",
                name="Fake Linear Regression",
                domain="optimization",
                subdomain="linear_models",
                applicable_scenarios=["linear baseline"],
                math_form="y = X beta",
                python_template="# placeholder",
                keywords=["regression", "linear", "least squares"],
            ),
        ]
    )


@pytest.fixture
def problem() -> ProblemInput:
    return ProblemInput(
        problem_text=(
            "Staff a call center modeled as a multi-server Markov queue with "
            "Poisson arrivals so wait times stay below a target."
        ),
        competition_type="mcm",
    )


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem=(
            "Determine the minimum number of agents c for an M/M/c call "
            "center so mean waiting time stays under the SLA."
        ),
        sub_questions=[
            "What is the steady-state queue length?",
            "How many servers meet the SLA?",
        ],
        proposed_approaches=[
            ApproachSketch(
                name="queueing",
                rationale="analytic closed form",
                methods=["M/M/c"],
            ),
        ],
    )


async def test_modeler_emits_hmml_log_and_prompt_carries_methods(
    fake_hmml: HMMLService, problem: ProblemInput, analysis: AnalyzerOutput
) -> None:
    spec_json = json.dumps(
        {
            "chosen_approach": "M/M/c queue",
            "rationale": "Poisson + exponential fits the scenario.",
            "variables": [
                {
                    "symbol": "\\lambda",
                    "name": "arrival rate",
                    "unit": "1/min",
                    "description": "mean calls per minute",
                },
            ],
            "equations": [
                {
                    "latex": "\\rho = \\lambda / (c \\mu)",
                    "description": "utilization",
                },
            ],
            "algorithm_outline": ["Estimate lambda,mu", "Solve for c"],
            "complexity_notes": "closed form",
            "validation_strategy": "Compare to discrete-event simulation.",
            "consulted_methods": [
                {
                    "id": "fake_mmc",
                    "name": "Fake M/M/c Queue",
                    "reason": "selected as primary",
                },
                {
                    "id": "fake_linreg",
                    "name": "Fake Linear Regression",
                    "reason": "considered but inferior — problem is stochastic",
                },
            ],
        }
    )

    gateway = _FakeGateway([spec_json])
    emitter = _FakeEmitter()

    agent = ModelerAgent(gateway, emitter, hmml=fake_hmml)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    # 1. ModelSpec returned and carries consulted_methods.
    assert isinstance(out, ModelSpec)
    assert len(out.consulted_methods) == 2
    consulted_ids = {c.id for c in out.consulted_methods}
    assert consulted_ids == {"fake_mmc", "fake_linreg"}
    assert any(c.reason == "selected as primary" for c in out.consulted_methods)

    # 2. A log event was emitted that mentions the retrieved method names.
    log_events = [e for e in emitter.events if e[0] == "log"]
    assert log_events, "expected at least one log event from Modeler"
    log_messages = " | ".join(str(e[1].get("message", "")) for e in log_events)
    assert "HMML retrieved" in log_messages
    assert "Fake M/M/c Queue" in log_messages

    # 3. The rendered user prompt carries the retrieved methods block.
    assert gateway.last_messages is not None
    user_msg = next(m for m in gateway.last_messages if m["role"] == "user")
    user_text = user_msg["content"]
    assert "Fake M/M/c Queue" in user_text
    assert "fake_mmc" in user_text
    assert "Canonical form" in user_text


async def test_modeler_works_without_hmml(
    problem: ProblemInput, analysis: AnalyzerOutput
) -> None:
    """When hmml=None, Modeler runs as before and emits no HMML log."""
    spec_json = json.dumps(
        {
            "chosen_approach": "M/M/1 queue",
            "rationale": "simplified analytic model.",
            "variables": [],
            "equations": [],
            "algorithm_outline": ["compute rho"],
            "complexity_notes": None,
            "validation_strategy": "simulation",
            "consulted_methods": [],
        }
    )
    gateway = _FakeGateway([spec_json])
    emitter = _FakeEmitter()

    agent = ModelerAgent(gateway, emitter, hmml=None)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert isinstance(out, ModelSpec)
    assert out.consulted_methods == []

    # No HMML log event when hmml is absent.
    log_events = [e for e in emitter.events if e[0] == "log"]
    assert not any(
        "HMML retrieved" in str(e[1].get("message", "")) for e in log_events
    )

    # User prompt should still render (with the no-HMML placeholder).
    assert gateway.last_messages is not None
    user_msg = next(m for m in gateway.last_messages if m["role"] == "user")
    assert "HMML knowledge base unavailable" in user_msg["content"]
