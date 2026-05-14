"""SKILL.md discovery, loosely modeled on Claude Code's skill loader.

Design notes
------------
The Claude Code skill system separates discovery from invocation: the
*frontmatter* (name + one-line description + when_to_use) is always present
in the system prompt at <1% context budget, while the full SKILL.md *body*
is only loaded when the agent actively decides to use the skill. We mirror
that two-layer design here.

A skill lives at ``<root>/<name>/SKILL.md`` (matching Claude Code's
directory format — single-file ``foo.md`` siblings are ignored). Symlinks
are followed, so existing docs like ``docs/matlab.md`` can be exposed
without being moved.

The loader is deliberately permissive about frontmatter shape — files
without a leading ``---`` block, or with malformed YAML, are skipped with
a warning instead of crashing the worker boot. Real-world skill content
authored by humans is the most likely failure mode and a single bad file
should not take the whole registry offline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # PyYAML is in the worker dep tree; the fallback is for tooling-only contexts.
    import yaml as _yaml

    _HAS_YAML = True
except ImportError:  # pragma: no cover - defensive
    _yaml = None  # type: ignore[assignment]
    _HAS_YAML = False


# Per-entry hard cap (matches Claude Code's MAX_LISTING_DESC_CHARS = 250).
# Anything longer than this wastes turn-1 cache_creation tokens without
# improving the discovery match-rate; the full SKILL.md is only ever
# loaded on demand.
DEFAULT_MAX_DESC_CHARS = 250


@dataclass(frozen=True)
class Skill:
    """A single SKILL.md entry.

    The fields up to ``arguments`` are the cheap discovery layer (live in
    the system prompt). ``body`` is the on-demand layer — kept in memory
    after load but only ever surfaced when the agent invokes the skill.
    """

    name: str
    description: str
    when_to_use: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    arguments: list[dict[str, Any]] = field(default_factory=list)
    context_mode: str = "inline"  # "inline" | "fork"
    body: str = ""
    source_path: Path = field(default_factory=lambda: Path())


class SkillRegistry:
    """In-memory map of skill name -> Skill, with deterministic ordering."""

    def __init__(self, skills: Iterable[Skill]):
        # Sort alphabetically by name for a stable, predictable discovery
        # listing — matters because the listing is rendered into the system
        # prompt and we want cache hits across runs with the same disk state.
        self._skills: dict[str, Skill] = {
            s.name: s for s in sorted(skills, key=lambda s: s.name)
        }

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills.keys())

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self):
        return iter(self._skills.values())

    def index_summary(self, max_chars_per_skill: int = DEFAULT_MAX_DESC_CHARS) -> str:
        """Cheap discovery block: ``- name: description`` per line.

        The description is truncated per-skill so a single verbose entry
        cannot blow the prompt budget. We DO NOT include the body or the
        when_to_use list here — those are only needed once the agent has
        decided to invoke a skill.
        """
        if not self._skills:
            return ""
        lines = []
        for s in self._skills.values():
            desc = _truncate(s.description, max_chars_per_skill)
            lines.append(f"- {s.name}: {desc}")
        return "\n".join(lines)


# ----------------------------------------------------------- frontmatter parsing


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body). frontmatter_text=None if missing."""
    if not content.startswith("---"):
        return None, content
    # Need the line after the leading '---' marker to start; find the
    # closing '---' on its own line.
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, content
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            # Drop a single leading blank line on the body for prettier
            # round-tripping; the SKILL.md authoring convention puts one
            # blank line between '---' and the H1.
            if body.startswith("\n"):
                body = body.lstrip("\n")
            return fm, body
    return None, content


def _parse_frontmatter_yaml(text: str) -> dict[str, Any]:
    """Parse with pyyaml when available, else a tiny inline parser.

    The inline fallback handles only the shapes we expect in a SKILL.md
    frontmatter: ``key: scalar``, ``key:`` followed by ``  - item`` block
    lists, and ``key: [a, b]`` inline lists. It is NOT a general YAML
    parser — anyone authoring frontmatter beyond that should install
    pyyaml.
    """
    if _HAS_YAML and _yaml is not None:
        data = _yaml.safe_load(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"frontmatter must be a YAML mapping, got {type(data).__name__}")
        return data

    return _parse_frontmatter_fallback(text)


def _parse_frontmatter_fallback(text: str) -> dict[str, Any]:
    """Minimal frontmatter parser used when pyyaml is unavailable."""
    out: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")) and current_list is not None:
            stripped = raw.strip()
            if stripped.startswith("- "):
                item = stripped[2:].strip().strip('"').strip("'")
                current_list.append(item)
            continue
        # New top-level key.
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current_key = key
            current_list = []
            out[key] = current_list
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
            out[key] = items
            current_list = None
            current_key = None
            continue
        # Scalar — strip surrounding quotes.
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
        current_list = None
        current_key = None
    # current_key is intentionally unused after the loop; the dict already
    # contains the list reference.
    del current_key
    return out


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _coerce_arguments(value: Any) -> list[dict[str, Any]]:
    """Arguments can be a list of dicts (full spec) or a list of strings (names only)."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"name": item})
        return out
    return []


def _skill_from_frontmatter(
    fm: dict[str, Any],
    body: str,
    source: Path,
    fallback_name: str,
) -> Skill | None:
    name = fm.get("name") or fallback_name
    if not isinstance(name, str) or not name.strip():
        return None
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        # A skill with no description is useless for discovery — skip it
        # rather than surface a blank entry to the model.
        return None
    context_raw = fm.get("context", "inline")
    context_mode = "fork" if str(context_raw).strip() == "fork" else "inline"

    # 'allowed-tools' is the Claude Code spelling; accept the snake_case too
    # to be friendly to hand-written frontmatter.
    allowed_tools_raw = fm.get("allowed-tools", fm.get("allowed_tools"))

    return Skill(
        name=name.strip(),
        description=description.strip(),
        when_to_use=_coerce_str_list(fm.get("when_to_use")),
        allowed_tools=_coerce_str_list(allowed_tools_raw),
        arguments=_coerce_arguments(fm.get("arguments")),
        context_mode=context_mode,
        body=body,
        source_path=source,
    )


# ---------------------------------------------------------------- public loader


def load_skills_dir(root: Path) -> SkillRegistry:
    """Walk *root* for ``<name>/SKILL.md`` files and return a SkillRegistry.

    Symlinks ARE followed — this is how ``docs/matlab.md`` is exposed
    without being moved. Files without a YAML frontmatter block, or with
    invalid YAML, are skipped with a warning; one bad file never breaks
    the registry.
    """
    if not root.exists() or not root.is_dir():
        logger.debug("skill root %s does not exist or is not a directory", root)
        return SkillRegistry([])

    skills: list[Skill] = []
    # iterdir() handles symlinks to directories naturally; sort for
    # deterministic load order across platforms.
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        # entry may be a symlink to a directory containing SKILL.md, OR a
        # symlink directly to a SKILL.md file (we allow both for ergonomics).
        if entry.is_symlink() and entry.resolve().is_file():
            skill_file = entry
            fallback_name = entry.name.removesuffix(".md")
        else:
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            fallback_name = entry.name

        if not skill_file.exists():
            continue
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[skills] failed to read %s: %s", skill_file, e)
            continue

        fm_text, body = _split_frontmatter(content)
        if fm_text is None:
            logger.warning("[skills] no frontmatter in %s, skipping", skill_file)
            continue
        try:
            fm = _parse_frontmatter_yaml(fm_text)
        except Exception as e:  # noqa: BLE001 — yaml raises a zoo of errors
            logger.warning("[skills] invalid frontmatter in %s: %s", skill_file, e)
            continue

        if not isinstance(fm, dict):
            logger.warning(
                "[skills] frontmatter in %s is not a mapping, skipping",
                skill_file,
            )
            continue

        skill = _skill_from_frontmatter(fm, body, skill_file, fallback_name)
        if skill is None:
            logger.warning(
                "[skills] frontmatter in %s missing required fields (name/description), skipping",
                skill_file,
            )
            continue
        skills.append(skill)

    return SkillRegistry(skills)


def format_index_for_prompt(registry: SkillRegistry, language: str = "en") -> str:
    """Render a token-efficient discovery block for the system prompt.

    Empty registry yields an empty string so callers can unconditionally
    concatenate without producing a dangling header.
    """
    if len(registry) == 0:
        return ""
    header = "## 可用技能" if language == "zh" else "## Available skills"
    body = registry.index_summary()
    return f"{header}\n{body}"


__all__ = [
    "DEFAULT_MAX_DESC_CHARS",
    "Skill",
    "SkillRegistry",
    "format_index_for_prompt",
    "load_skills_dir",
]
