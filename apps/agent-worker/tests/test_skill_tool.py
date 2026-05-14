"""Tests for the on-demand skill loading tool (`get_skill`).

Covers:
- `SkillTool.handle` returns the body for a valid name.
- `SkillTool.handle` returns a structured error for unknown / blank names.
- `SkillRegistry.render_menu` produces frontmatter-only output.
- BaseAgent system prompt assembly: bodies are omitted when
  ``use_skill_tool=True`` and included by default otherwise.
- CoderAgent prompt assembly: bodies are omitted when the skill tool
  is enabled, included via menu only.
- Integration: a mocked Coder turn requests a skill, receives the body,
  and proceeds to execute code on the next turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from agent_worker.agents import CoderAgent
from agent_worker.kernel import KernelSession
from agent_worker.skills import (
    Skill,
    SkillRegistry,
    SkillTool,
    SkillToolResult,
    build_get_skill_tool_spec,
    render_skill_menu,
)
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    ModelSpec,
    ProblemInput,
)

# ---------------------------------------------------------- registry helpers


def _make_registry() -> SkillRegistry:
    """Two-entry registry with body markers that do NOT collide with Coder prompt.

    The Coder system prompt itself mentions octave + matplotlib, so we
    use distinctive sentinel strings inside the body that we can grep
    for without false positives.
    """
    return SkillRegistry(
        [
            Skill(
                name="matlab_x",
                description="MATLAB cell execution patterns for Coder.",
                when_to_use=[
                    "Coder picks language='matlab'",
                    "ODE solving",
                ],
                body="# MATLAB skill body\n\nSENTINEL-BODY-MATLAB-XYZ123\n",
            ),
            Skill(
                name="figures_x",
                description="Figure-saving conventions: savefig.",
                when_to_use=["any figure"],
                body="# Figures body\n\nSENTINEL-BODY-FIGURES-XYZ456\n",
            ),
        ]
    )


# ---------------------------------------------------------- SkillTool basics


def test_get_skill_returns_body_for_valid_name() -> None:
    reg = _make_registry()
    tool = SkillTool(reg)

    result = tool.handle({"name": "matlab_x"})

    assert isinstance(result, SkillToolResult)
    assert result.ok is True
    assert result.name == "matlab_x"
    assert "MATLAB skill body" in result.content
    assert "SENTINEL-BODY-MATLAB-XYZ123" in result.content


def test_get_skill_returns_error_for_unknown_name() -> None:
    reg = _make_registry()
    tool = SkillTool(reg)

    result = tool.handle({"name": "does_not_exist"})

    assert result.ok is False
    assert result.name == "does_not_exist"
    # Error message must mention what the name was and what is available
    # so the model can retry with a corrected name.
    assert "does_not_exist" in result.content
    assert "matlab_x" in result.content
    assert "figures_x" in result.content
    assert "Error" in result.content


def test_get_skill_handles_missing_or_blank_args() -> None:
    reg = _make_registry()
    tool = SkillTool(reg)

    # None args.
    res_none = tool.handle(None)
    assert res_none.ok is False
    assert "non-empty" in res_none.content or "required" in res_none.content.lower()

    # Missing key.
    res_missing = tool.handle({})
    assert res_missing.ok is False

    # Blank string.
    res_blank = tool.handle({"name": "   "})
    assert res_blank.ok is False

    # Wrong type.
    res_int = tool.handle({"name": 42})
    assert res_int.ok is False


def test_get_skill_tool_spec_shape() -> None:
    spec = build_get_skill_tool_spec()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "get_skill"
    params = spec["function"]["parameters"]
    assert params["type"] == "object"
    assert "name" in params["properties"]
    assert params["required"] == ["name"]
    assert params["additionalProperties"] is False


def test_skill_tool_name_property() -> None:
    reg = _make_registry()
    tool = SkillTool(reg)
    assert tool.name == "get_skill"
    # tool_spec returns a fresh dict each call (model providers complain
    # about shared mutable dicts in request bodies).
    assert tool.tool_spec() is not tool.tool_spec()


# --------------------------------------------------------- render_menu / registry


def test_render_menu_produces_frontmatter_only_output() -> None:
    reg = _make_registry()
    menu = render_skill_menu(reg)

    # Menu has headers + descriptions + when_to_use, but NO body content.
    assert "Available skills" in menu
    assert "### matlab_x" in menu
    assert "### figures_x" in menu
    assert "MATLAB cell execution patterns" in menu
    assert "Coder picks language='matlab'" in menu

    # Critical: bodies must NOT appear in the menu — that's the whole
    # point of the on-demand tool path.
    assert "SENTINEL-BODY-MATLAB-XYZ123" not in menu
    assert "SENTINEL-BODY-FIGURES-XYZ456" not in menu


def test_render_menu_via_registry_method() -> None:
    reg = _make_registry()
    direct = render_skill_menu(reg)
    via_method = reg.render_menu()
    assert direct == via_method


def test_render_menu_empty_registry_returns_empty_string() -> None:
    reg = SkillRegistry([])
    assert render_skill_menu(reg) == ""
    assert reg.render_menu() == ""


def test_render_menu_zh_language() -> None:
    reg = _make_registry()
    menu = reg.render_menu(language="zh")
    assert "可用技能" in menu
    # Body still absent regardless of language.
    assert "SENTINEL-BODY-MATLAB-XYZ123" not in menu


# --------------------------------------------------------- BaseAgent assembly


def test_base_agent_skill_tool_flag_off_omits_menu(tmp_path: Path) -> None:
    """When the flag is off, the system prompt is unchanged."""
    from agent_worker.agents import AnalyzerAgent

    # Construct without any of the LLM machinery — we only inspect prompt
    # assembly, never call run().
    agent = AnalyzerAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        skill_registry=_make_registry(),
        use_skill_tool=False,  # explicit
    )
    assembled = agent._system_prompt_text()
    assert "Available skills" not in assembled
    # And the original system prompt is intact.
    assert assembled == agent.prompt.system["text"]
    assert agent.skill_tool is None


def test_base_agent_skill_tool_flag_on_appends_menu_omits_body(tmp_path: Path) -> None:
    """When the flag is on AND a registry is provided, the menu is appended."""
    from agent_worker.agents import AnalyzerAgent

    agent = AnalyzerAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        skill_registry=_make_registry(),
        use_skill_tool=True,
    )
    assembled = agent._system_prompt_text()
    assert "Available skills" in assembled
    assert "### matlab_x" in assembled
    # Critical: bodies must not appear in the system prompt.
    assert "SENTINEL-BODY-MATLAB-XYZ123" not in assembled
    assert "SENTINEL-BODY-FIGURES-XYZ456" not in assembled
    assert agent.skill_tool is not None


def test_base_agent_skill_tool_on_but_empty_registry_no_op(tmp_path: Path) -> None:
    """Empty registry + flag-on must not append a dangling header."""
    from agent_worker.agents import AnalyzerAgent

    agent = AnalyzerAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        skill_registry=SkillRegistry([]),
        use_skill_tool=True,
    )
    assembled = agent._system_prompt_text()
    assert "Available skills" not in assembled
    assert assembled == agent.prompt.system["text"]
    assert agent.skill_tool is None


def test_base_agent_skill_tool_default_off() -> None:
    """No-arg construction inherits the safe default: tool disabled."""
    from agent_worker.agents import AnalyzerAgent

    agent = AnalyzerAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
    )
    assert agent.skill_tool is None
    assert agent._system_prompt_text() == agent.prompt.system["text"]


# --------------------------------------------------------- Coder prompt assembly


def test_coder_prompt_assembly_omits_skill_bodies_when_flag_on(tmp_path: Path) -> None:
    """Coder's system prompt carries the menu, never the bodies."""
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        kernel=kernel,
        skill_registry=_make_registry(),
        use_skill_tool=True,
    )
    assembled = agent._system_prompt_text()
    assert "Available skills" in assembled
    assert "### matlab_x" in assembled
    # No bodies leaked.
    assert "SENTINEL-BODY-MATLAB-XYZ123" not in assembled
    assert "SENTINEL-BODY-FIGURES-XYZ456" not in assembled


def test_coder_prompt_assembly_eager_baseline_when_flag_off(tmp_path: Path) -> None:
    """With the flag off, Coder system prompt is unchanged."""
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        kernel=kernel,
        skill_registry=_make_registry(),
        use_skill_tool=False,
    )
    assembled = agent._system_prompt_text()
    assert "Available skills" not in assembled
    assert assembled == agent.prompt.system["text"]
    assert agent.skill_tool is None


def test_coder_skill_tool_enabled_by_default_when_registry_passed(
    tmp_path: Path,
) -> None:
    """Coder is the first agent to opt in — default ``use_skill_tool=True``."""
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        kernel=kernel,
        skill_registry=_make_registry(),
    )
    assert agent.skill_tool is not None
    assert "Available skills" in agent._system_prompt_text()


def test_coder_without_registry_runs_identically_to_baseline(tmp_path: Path) -> None:
    """No registry → tool stays None, prompt unchanged."""
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(
        gateway=None,  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        kernel=kernel,
    )
    assert agent.skill_tool is None
    assert agent._system_prompt_text() == agent.prompt.system["text"]


# --------------------------------------------------------- Coder integration


class _FakeEmitter:
    """Same shape as the test_coder_agent harness."""

    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(
        self, kind: str, payload: dict | None = None, agent: str | None = None
    ) -> None:
        self.events.append((kind, payload or {}, agent))


class _ScriptedGateway:
    """Yields a queued list of JSON strings, one per stream_completion() call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream_completion(
        self, *, messages: list[dict[str, Any]], **_: object
    ) -> AsyncIterator[str]:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("gateway out of scripted responses")
        chunk = self._responses.pop(0)
        for ch in [chunk]:
            yield ch

    async def close(self) -> None:
        pass


@pytest.fixture
def problem() -> ProblemInput:
    return ProblemInput(problem_text="Compute 1+2 and print the answer.")


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Print the integer seven using Python.",
        sub_questions=["what is the answer"],
        proposed_approaches=[
            ApproachSketch(
                name="direct",
                rationale="trivial arithmetic",
                methods=["arithmetic"],
            )
        ],
    )


@pytest.fixture
def spec() -> ModelSpec:
    return ModelSpec(
        chosen_approach="direct arithmetic in Python",
        rationale="trivial problem with one operation",
        algorithm_outline=["compute the value", "print result"],
        validation_strategy="visual inspection of output",
    )


async def test_coder_skill_tool_roundtrip(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    """Mocked Coder turn calls get_skill, receives body, then executes code.

    Sequence:
    1. Turn 1: directive sets ``skill_request="matlab_x"`` — no code yet.
       Loop dispatches SkillTool, splices body into messages, re-asks
       WITHOUT advancing the iteration counter.
    2. Turn 2: directive with ``skill_request=null`` and ``done=true``,
       real code that prints 7.
    3. Output has exactly one executed cell; the messages transcript
       contains the skill body verbatim (proof it landed in context).
    """
    reg = _make_registry()
    gateway = _ScriptedGateway(
        [
            # First call: ask for the matlab skill body.
            (
                '{"reasoning":"need matlab patterns",'
                '"code":"",'
                '"done":false,'
                '"summary":null,'
                '"skill_request":"matlab_x"}'
            ),
            # Second call: now armed with the body, execute the real code.
            (
                '{"reasoning":"got the skill, run the code",'
                '"code":"print(7)",'
                '"done":true,'
                '"summary":"Printed 7."}'
            ),
        ]
    )
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(
        gateway,  # type: ignore[arg-type]
        emitter,  # type: ignore[arg-type]
        kernel,
        skill_registry=reg,
    )
    output = await agent.run(problem, analysis, spec)

    # Real iteration count was 1 — the skill lookup must NOT have burned an iteration.
    assert len(gateway.calls) == 2, "expected two LLM calls (one lookup, one exec)"
    assert len(output.cells) == 1
    assert "7" in output.cells[0].stdout
    assert output.final_summary == "Printed 7."

    # The skill body was spliced into the message transcript before turn 2.
    second_call_messages = gateway.calls[1]
    transcript = "\n".join(
        m.get("content", "") for m in second_call_messages if isinstance(m, dict)
    )
    assert "SENTINEL-BODY-MATLAB-XYZ123" in transcript, (
        "skill body must appear in transcript so subsequent turns see it"
    )

    # A `log` event records the skill_tool dispatch.
    log_events = [e for e in emitter.events if e[0] == "log"]
    skill_logs = [
        e for e in log_events if "skill_tool" in e[1].get("message", "")
    ]
    assert skill_logs, "expected at least one skill_tool log event"
    assert "matlab_x" in skill_logs[0][1]["message"]


async def test_coder_skill_tool_unknown_name_error_recovery(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    """Unknown skill name produces a structured error and the loop continues."""
    reg = _make_registry()
    gateway = _ScriptedGateway(
        [
            # Ask for a nonexistent skill.
            (
                '{"reasoning":"oops typo",'
                '"code":"",'
                '"done":false,'
                '"summary":null,'
                '"skill_request":"matlab_z"}'
            ),
            # Recover and execute.
            (
                '{"reasoning":"recovered",'
                '"code":"print(\\"ok\\")",'
                '"done":true,'
                '"summary":"recovered"}'
            ),
        ]
    )
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)
    agent = CoderAgent(
        gateway,  # type: ignore[arg-type]
        emitter,  # type: ignore[arg-type]
        kernel,
        skill_registry=reg,
    )
    output = await agent.run(problem, analysis, spec)

    assert len(output.cells) == 1
    assert output.final_summary == "recovered"
    # Error message landed in transcript so the model knew to retry.
    transcript = "\n".join(
        m.get("content", "")
        for m in gateway.calls[1]
        if isinstance(m, dict)
    )
    assert "matlab_z" in transcript
    assert "Available skills" in transcript or "Error" in transcript
