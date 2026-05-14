"""Skill registry — SKILL.md discovery for the agent worker.

See ``loader.py`` for the design notes on the two-layer (frontmatter vs.
body) pattern borrowed from Claude Code.
"""

from agent_worker.skills.loader import (
    Skill,
    SkillRegistry,
    format_index_for_prompt,
    load_skills_dir,
)

__all__ = [
    "Skill",
    "SkillRegistry",
    "format_index_for_prompt",
    "load_skills_dir",
]
