from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from agent_worker.agents import CriticAgent
from mm_contracts import AnalyzerOutput, ApproachSketch, CritiqueReport


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
        for chunk in self._chunks:
            yield chunk


async def test_critic_agent_reviews_analyzer_output() -> None:
    report_json = (
        '{"target_agent":"analyzer",'
        '"target_schema":"AnalyzerOutput",'
        '"passed":true,'
        '"score":0.88,'
        '"summary":"Analysis covers the problem and has usable approaches.",'
        '"findings":[],'
        '"required_changes":[]}'
    )
    emitter = _FakeEmitter()
    gateway = _FakeGateway([report_json])
    critic = CriticAgent(gateway, emitter)  # type: ignore[arg-type]
    artifact = AnalyzerOutput(
        restated_problem="A 20-character restatement of the modeling problem.",
        sub_questions=["Estimate demand", "Optimize allocation"],
        proposed_approaches=[
            ApproachSketch(
                name="Optimization", rationale="Fits allocation", methods=["LP"]
            )
        ],
    )

    report = await critic.review(
        target_agent="analyzer",
        target_schema="AnalyzerOutput",
        artifact=artifact.model_dump(mode="json"),
        context={"problem_text": "Optimize allocation under demand uncertainty."},
        criteria=["covers all sub-questions", "lists usable approaches"],
    )

    assert isinstance(report, CritiqueReport)
    assert report.passed is True
    assert report.score == 0.88
    assert [event[0] for event in emitter.events] == [
        "stage.start",
        "agent.output",
        "stage.done",
    ]
    assert all(event[2] == "critic" for event in emitter.events)
