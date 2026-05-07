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
        self.calls: list[dict[str, object]] = []

    async def stream_completion(self, **kwargs: object) -> AsyncIterator[str]:
        self.calls.append(kwargs)
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
        '"required_changes":[],'
        '"roles":[{"role":"modeling_coach","passed":true,"score":0.9,'
        '"summary":"Modeling decomposition is usable.","findings":[]}],'
        '"checklist":[{"id":"coverage","label":"Covers all sub-questions",'
        '"passed":true,"evidence":"Both demand and allocation are listed."}],'
        '"revision_round":0,'
        '"max_revision_rounds":2,'
        '"budget_exhausted":false}'
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
    assert report.roles[0].role == "modeling_coach"
    assert report.checklist[0].id == "coverage"
    assert [event[0] for event in emitter.events] == [
        "stage.start",
        "agent.output",
        "stage.done",
    ]
    assert all(event[2] == "critic" for event in emitter.events)


async def test_critic_agent_sends_role_and_checklist_inputs() -> None:
    report_json = (
        '{"target_agent":"coder",'
        '"target_schema":"CoderOutput",'
        '"passed":true,'
        '"score":0.91,'
        '"summary":"Code output is executable and numerically useful.",'
        '"findings":[],'
        '"required_changes":[],'
        '"roles":[{"role":"code_reviewer","passed":true,"score":0.91,'
        '"summary":"Code is bounded and reproducible.","findings":[]}],'
        '"checklist":[{"id":"execution_support","label":"Executed cells support the model.",'
        '"passed":true,"evidence":"Notebook cells produce summary metrics."}],'
        '"revision_round":1,'
        '"max_revision_rounds":2,'
        '"budget_exhausted":false}'
    )
    emitter = _FakeEmitter()
    gateway = _FakeGateway([report_json])
    critic = CriticAgent(gateway, emitter)  # type: ignore[arg-type]

    await critic.review(
        target_agent="coder",
        target_schema="CoderOutput",
        artifact={"summary": "baseline objective = 12.4"},
        context={"problem_text": "Optimize allocation under demand uncertainty."},
        criteria=["Executed cells support the model specification."],
        revision_round=1,
        max_revision_rounds=2,
    )

    messages = gateway.calls[0]["messages"]
    assert isinstance(messages, list)
    user_message = messages[1]["content"]  # type: ignore[index]
    assert "Roles:" in user_message
    assert "- modeling_coach" in user_message
    assert "- code_reviewer" in user_message
    assert "Checklist:" in user_message
    assert "execution_support" in user_message
    assert "Revision round: 1 / 2" in user_message
