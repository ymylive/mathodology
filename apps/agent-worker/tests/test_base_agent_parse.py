"""`_parse_output` — the JSON-fence-stripping + Pydantic-validation helper.

We sidestep the full agent lifecycle (gateway + emitter) by constructing a
BaseAgent subclass with a local prompt and calling `_parse_output` directly.
"""

from __future__ import annotations

import pytest
from agent_worker.agents.base import AgentParseError, BaseAgent
from mm_contracts import AnalyzerOutput

_VALID_JSON = (
    '{"restated_problem":"A 20-char problem.",'
    '"sub_questions":["q"],'
    '"proposed_approaches":[{"name":"n","rationale":"r","methods":[]}]}'
)


class _DummyAnalyzer(BaseAgent):
    AGENT_NAME = "analyzer"
    OUTPUT_MODEL = AnalyzerOutput

    def __init__(self) -> None:  # noqa: D401 — bypass real prompt/gateway deps
        # Do NOT call super().__init__(); we don't need gateway/emitter/prompt
        # for the pure `_parse_output` unit test.
        pass


def _agent() -> _DummyAnalyzer:
    return _DummyAnalyzer()


def test_parse_plain_json() -> None:
    out = _agent()._parse_output(_VALID_JSON)
    assert isinstance(out, AnalyzerOutput)
    assert out.restated_problem == "A 20-char problem."
    assert out.sub_questions == ["q"]
    assert out.proposed_approaches[0].name == "n"


def test_parse_strips_markdown_fence() -> None:
    fenced = f"```json\n{_VALID_JSON}\n```"
    out = _agent()._parse_output(fenced)
    assert isinstance(out, AnalyzerOutput)
    assert out.restated_problem == "A 20-char problem."


def test_parse_strips_bare_fence() -> None:
    fenced = f"```\n{_VALID_JSON}\n```"
    out = _agent()._parse_output(fenced)
    assert isinstance(out, AnalyzerOutput)


def test_parse_invalid_json_raises() -> None:
    with pytest.raises(AgentParseError):
        _agent()._parse_output("not a json object at all")


def test_parse_missing_required_field_raises() -> None:
    # Missing `proposed_approaches` and others.
    bad = '{"restated_problem":"A 20-char problem.","sub_questions":["q"]}'
    with pytest.raises(AgentParseError):
        _agent()._parse_output(bad)


def test_parse_extra_field_rejected() -> None:
    # AnalyzerOutput has `extra="forbid"`, so unknown keys raise.
    bad = (
        '{"restated_problem":"A 20-char problem.",'
        '"sub_questions":["q"],'
        '"proposed_approaches":[{"name":"n","rationale":"r","methods":[]}],'
        '"surprise":"x"}'
    )
    with pytest.raises(AgentParseError):
        _agent()._parse_output(bad)
