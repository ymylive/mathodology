"""On-demand skill body loading via a `get_skill` tool.

Background
----------
``SkillRegistry`` ships two layers (see ``loader.py``): cheap frontmatter
discovery in the system prompt, plus a full markdown ``body`` that is only
worth surfacing when the agent has actually decided to invoke a skill.

Round-7 prompt caching tames the system-prompt cost somewhat, but eagerly
inlining every skill body still has two real costs:

1. The system prompt grows past the *optimal* cache boundary. For the
   Coder, which is the only agent today with 8+ kernel/MATLAB skills, the
   bodies routinely add 5-10k tokens that the LLM rarely consumes in any
   single turn.
2. Discovery quality drops: the model sees a wall of text instead of a
   clean menu and has to scan the whole prefix to remember what is
   available.

This module exposes the body behind an OpenAI-style ``get_skill`` tool.
The agent's system prompt carries only the *menu* (one line per skill,
plus a short `when_to_use` block) and lists ``get_skill`` as a callable
tool. When the model decides a skill is relevant it emits a tool call;
the registry returns the raw markdown body which enters the conversation
as a tool result — cacheable within the run, scoped to the single turn
where it was needed.

Wire-up
-------
``BaseAgent`` consumes ``SkillTool`` via two constructor knobs
(``skill_registry`` + ``use_skill_tool``). When the flag is False (the
default) nothing about the existing prompt pipeline changes — the menu is
not rendered and the tool is not exposed. Per-agent gradual rollout is
the whole point of the flag; we flip it for the Coder in this PR because
Coder has the most skills and the worst eager-load tax, and leave the
other agents on the eager path until we have signal that the tool path
is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_worker.skills.loader import Skill, SkillRegistry

# Public tool name — kept short for token efficiency in the system prompt's
# tool listing. Matches the convention sourcemap uses (``view_skill`` in
# their tree) but renamed to ``get_skill`` to match the surrounding
# Mathodology vocabulary ("get" verbs everywhere in the worker tools dir).
GET_SKILL_TOOL_NAME = "get_skill"


def build_get_skill_tool_spec() -> dict[str, Any]:
    """Return the OpenAI-style JSONSchema spec for the ``get_skill`` tool.

    The shape matches what an OpenAI-compat / Anthropic-compat gateway
    expects in ``tools=[{type: "function", function: {...}}]``. Keeping
    this in a factory (rather than a module constant) lets us pin the
    schema once per turn — model providers occasionally complain about
    shared mutable dicts in their request bodies.
    """
    return {
        "type": "function",
        "function": {
            "name": GET_SKILL_TOOL_NAME,
            "description": (
                "Load the full body of a skill by name. The menu in the system "
                "prompt lists every skill's name, short description, and "
                "when_to_use triggers; call this tool only when one of those "
                "triggers matches the current task. The tool result is the raw "
                "markdown body of the skill."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Exact skill name as listed in the system prompt menu. "
                            "Case-sensitive; do not pass a path or extension."
                        ),
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


@dataclass
class SkillToolResult:
    """Tagged return type for ``SkillTool.handle``.

    Using a dataclass instead of bare strings lets callers tell success
    apart from a structured error (e.g. "unknown skill") without parsing
    the body — handy when the agent loop wants to emit a different event
    on failure.
    """

    ok: bool
    name: str
    content: str


class SkillTool:
    """Thin runtime adapter around ``SkillRegistry`` for tool-call dispatch.

    The class is intentionally tiny: it owns no state besides the
    registry reference and the canonical tool spec. Construction is
    cheap, so per-agent or per-run instances are both fine.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    @property
    def name(self) -> str:
        return GET_SKILL_TOOL_NAME

    def tool_spec(self) -> dict[str, Any]:
        """OpenAI-compat function spec — copy-on-read so callers can mutate."""
        return build_get_skill_tool_spec()

    def handle(self, arguments: dict[str, Any] | None) -> SkillToolResult:
        """Dispatch a tool call. Never raises — errors become structured results.

        Callers (the agent loop) take the ``content`` field verbatim as
        the tool message in the next turn. We deliberately do not raise
        on bad input: a noisy stack trace from a malformed tool call
        would break the run, whereas an error string lets the model
        recover (retry with a corrected name).
        """
        args = arguments or {}
        name_raw = args.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            return SkillToolResult(
                ok=False,
                name="",
                content=(
                    "Error: `get_skill` requires a non-empty string `name` "
                    "argument. Check the skill menu in the system prompt and "
                    "retry."
                ),
            )
        name = name_raw.strip()
        skill = self.registry.get(name)
        if skill is None:
            available = self.registry.names()
            preview = ", ".join(available[:10])
            if len(available) > 10:
                preview += ", …"
            return SkillToolResult(
                ok=False,
                name=name,
                content=(
                    f"Error: no skill named {name!r}. "
                    f"Available skills: [{preview}]. "
                    "Skill names are case-sensitive — copy from the menu."
                ),
            )
        return SkillToolResult(ok=True, name=skill.name, content=skill.body)


def render_skill_menu(
    registry: SkillRegistry,
    language: str = "en",
) -> str:
    """Render the agent-facing menu (frontmatter only — no skill bodies).

    Format::

        ## Available skills (call `get_skill` to load body)
        ### <name>
        <description>
        when_to_use:
          - <trigger 1>
          - <trigger 2>

    The header explicitly names ``get_skill`` so the model has the link
    between the menu and the tool one block away in context. The
    when_to_use bullets are the *trigger* hints the model uses to decide
    whether to call the tool — those have to live in the menu, not behind
    the tool, otherwise the model has nothing to dispatch from.
    """
    if len(registry) == 0:
        return ""
    if language == "zh":
        header = (
            "## 可用技能（调用 `get_skill` 加载正文）"
        )
    else:
        header = "## Available skills (call `get_skill` to load body)"
    lines: list[str] = [header]
    for s in registry:
        lines.append(f"### {s.name}")
        lines.append(s.description)
        if s.when_to_use:
            lines.append("when_to_use:")
            for trig in s.when_to_use:
                lines.append(f"  - {trig}")
        lines.append("")  # blank line between entries
    # Trim trailing blank line for tidier concatenation.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


__all__ = [
    "GET_SKILL_TOOL_NAME",
    "Skill",
    "SkillTool",
    "SkillToolResult",
    "build_get_skill_tool_spec",
    "render_skill_menu",
]
