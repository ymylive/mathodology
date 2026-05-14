"""Skill registry — SKILL.md discovery for the agent worker.

See ``loader.py`` for the design notes on the two-layer (frontmatter vs.
body) pattern borrowed from Claude Code. ``tool.py`` adds the on-demand
``get_skill`` tool that pulls a single body into the conversation only
when the agent decides to use it.
"""

from agent_worker.skills.loader import (
    Skill,
    SkillRegistry,
    format_index_for_prompt,
    load_skills_dir,
)
from agent_worker.skills.tool import (
    GET_SKILL_TOOL_NAME,
    SkillTool,
    SkillToolResult,
    build_get_skill_tool_spec,
    render_skill_menu,
)

__all__ = [
    "GET_SKILL_TOOL_NAME",
    "Skill",
    "SkillRegistry",
    "SkillTool",
    "SkillToolResult",
    "build_get_skill_tool_spec",
    "format_index_for_prompt",
    "load_skills_dir",
    "render_skill_menu",
]
