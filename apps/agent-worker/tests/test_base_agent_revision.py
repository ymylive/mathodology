from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from agent_worker.agents.base import BaseAgent
from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueFinding, CritiqueReport


class _DummyAnalyzer(BaseAgent):
    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput


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
        yield self._chunks.pop(0)


async def test_revise_with_critique_returns_validated_output() -> None:
    revised_json = (
        '{"restated_problem":"A revised 20-character restatement.",'
        '"sub_questions":["Estimate demand","Optimize allocation"],'
        '"assumptions":["Demand observations are representative."],'
        '"data_requirements":[],'
        '"proposed_approaches":[{"name":"Robust LP","rationale":"Handles uncertainty","methods":["LP"]}]}'
    )
    agent = _DummyAnalyzer(
        _FakeGateway([revised_json]), _FakeEmitter()  # type: ignore[arg-type]
    )
    original = AnalyzerOutput(
        restated_problem="A weak 20-character restatement.",
        sub_questions=["Estimate demand"],
        proposed_approaches=[
            ApproachSketch(name="LP", rationale="Simple", methods=["LP"])
        ],
    )
    critique = CritiqueReport(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        passed=False,
        score=0.5,
        summary="Missing one required sub-question.",
        findings=[
            CritiqueFinding(
                severity="major",
                area="coverage",
                message="The allocation sub-question is missing.",
                evidence="sub_questions only includes demand.",
                required_change="Add allocation optimization as a sub-question.",
            )
        ],
        required_changes=["Add allocation optimization."],
    )

    revised = await agent.revise_with_critique(
        original_output=original,
        critique=critique,
        context={"problem_text": "Estimate demand and optimize allocation."},
    )

    assert isinstance(revised, AnalyzerOutput)
    assert revised.sub_questions == ["Estimate demand", "Optimize allocation"]
