"""Prompt loader + user-template rendering."""

from __future__ import annotations

import pytest
from agent_worker.prompts import PromptSpec, load_prompt


def test_load_analyzer_v1_fields() -> None:
    spec = load_prompt("analyzer")
    assert isinstance(spec, PromptSpec)
    assert spec.version == "1.0.0"
    assert spec.agent == "analyzer"
    assert spec.model_preference[0] == "deepseek-chat"
    assert spec.token_budget_in == 8000
    assert spec.token_budget_out == 4000
    assert spec.temperature == pytest.approx(0.2)
    assert "Analyzer" in spec.system["text"]
    assert "{{ problem_text }}" in spec.user_template["text"]
    assert spec.response_schema == {"kind": "json_object", "name": "AnalyzerOutput"}


def test_load_analyzer_explicit_version() -> None:
    spec = load_prompt("analyzer", "v1")
    assert spec.version == "1.0.0"


def test_load_missing_prompt_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("nope", "v1")


def test_render_user_substitutes_vars() -> None:
    spec = load_prompt("analyzer")
    rendered = spec.render_user(
        problem_text="x",
        competition_type="mcm",
        attachments_summary="(none)",
    )
    assert "Problem type: mcm" in rendered
    assert "x" in rendered
    assert "(none)" in rendered
    assert "{{" not in rendered


def test_render_user_missing_vars_become_empty() -> None:
    spec = load_prompt("analyzer")
    rendered = spec.render_user(problem_text="only this")
    # `{{ competition_type }}` and `{{ attachments_summary }}` should vanish
    # rather than raise — pipeline code shouldn't crash on empty optionals.
    assert "{{" not in rendered
    assert "only this" in rendered
