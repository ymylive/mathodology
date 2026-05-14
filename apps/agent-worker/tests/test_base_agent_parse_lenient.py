"""Tests for the sourcemap-inspired lenient parse + LLM-friendly errors.

Covers:
- raw_decode prefix recovery when the model emits valid JSON followed by junk
- LLM-friendly Pydantic error formatting (matches sourcemap formatZodValidationError)
"""

from __future__ import annotations

import pytest
from agent_worker.agents.base import (
    AgentParseError,
    _format_validation_error,
    _parse_json_lenient,
    _strip_json_fence,
)
from pydantic import BaseModel, ConfigDict, ValidationError


class _Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    count: int


# --- _strip_json_fence -------------------------------------------------------


def test_strip_fence_with_json_tag() -> None:
    assert _strip_json_fence("```json\n{\"a\":1}\n```") == '{"a":1}'


def test_strip_fence_plain() -> None:
    assert _strip_json_fence("```\n{\"a\":1}\n```") == '{"a":1}'


def test_strip_fence_no_fence_is_passthrough() -> None:
    assert _strip_json_fence('  {"a":1}  ') == '{"a":1}'


# --- _parse_json_lenient -----------------------------------------------------


def test_parse_lenient_strict_path() -> None:
    obj = _parse_json_lenient('{"a": 1, "b": "x"}')
    assert obj == {"a": 1, "b": "x"}


def test_parse_lenient_recovers_prefix_when_trailing_prose() -> None:
    """The single most common gpt-5.5 long-generation failure mode."""
    blob = '{"a": 1, "b": "x"}\n\nNote: the above represents …'
    obj = _parse_json_lenient(blob)
    assert obj == {"a": 1, "b": "x"}


def test_parse_lenient_raises_on_truly_invalid_json() -> None:
    with pytest.raises(AgentParseError) as exc:
        _parse_json_lenient("not even close to JSON")
    assert "raw_decode failed" in str(exc.value)


def test_parse_lenient_recovers_when_trailing_braces() -> None:
    # Common when the model emits a JSON object then a code-fence-like tail.
    obj = _parse_json_lenient('{"k": [1, 2, 3]}```')
    assert obj == {"k": [1, 2, 3]}


# --- _format_validation_error -----------------------------------------------


def test_format_validation_error_missing_field() -> None:
    try:
        _Sample.model_validate({"count": 3})
    except ValidationError as e:
        out = _format_validation_error(e)
    assert "validation issue" in out
    assert "Required field `name` is missing" in out


def test_format_validation_error_type_mismatch() -> None:
    try:
        _Sample.model_validate({"name": "x", "count": "not-an-int"})
    except ValidationError as e:
        out = _format_validation_error(e)
    assert "`count`" in out
    # Pydantic v2's int-parse-failure has type "int_parsing"; we want the
    # offending value mentioned somewhere in the rewritten message.
    assert "integer" in out.lower() or "not-an-int" in out


def test_format_validation_error_extra_forbidden() -> None:
    try:
        _Sample.model_validate({"name": "x", "count": 1, "extra": True})
    except ValidationError as e:
        out = _format_validation_error(e)
    assert "Unexpected field `extra`" in out


def test_format_validation_error_nested_path() -> None:
    class _Nested(BaseModel):
        items: list[_Sample]

    try:
        _Nested.model_validate({"items": [{"name": "ok", "count": 1}, {"name": "ok"}]})
    except ValidationError as e:
        out = _format_validation_error(e)
    # The missing `count` lives at items.[1].count
    assert "[1]" in out and "count" in out
