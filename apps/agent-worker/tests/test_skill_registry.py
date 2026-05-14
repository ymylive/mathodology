"""Tests for the SKILL.md discovery layer."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from agent_worker.skills import (
    Skill,
    SkillRegistry,
    format_index_for_prompt,
    load_skills_dir,
)


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "") -> Path:
    """Helper: write a ``<root>/<name>/SKILL.md`` with the given frontmatter+body."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(f"---\n{frontmatter.strip()}\n---\n\n{body}", encoding="utf-8")
    return f


def test_empty_dir_returns_empty_registry(tmp_path: Path) -> None:
    reg = load_skills_dir(tmp_path)
    assert reg.names() == []
    assert len(reg) == 0
    assert reg.get("anything") is None
    # Empty registry must produce empty discovery block — caller-friendly.
    assert format_index_for_prompt(reg) == ""


def test_missing_root_returns_empty_registry(tmp_path: Path) -> None:
    reg = load_skills_dir(tmp_path / "does-not-exist")
    assert reg.names() == []


def test_valid_skill_loads_with_fields_populated(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "alpha",
        """
name: alpha
description: First skill in the registry.
when_to_use:
  - "every Monday"
  - "when the moon is full"
allowed-tools:
  - run_python
context: inline
""",
        body="# Alpha\nBody content here.\n",
    )
    reg = load_skills_dir(tmp_path)
    assert reg.names() == ["alpha"]
    skill = reg.get("alpha")
    assert skill is not None
    assert isinstance(skill, Skill)
    assert skill.name == "alpha"
    assert skill.description == "First skill in the registry."
    assert skill.when_to_use == ["every Monday", "when the moon is full"]
    assert skill.allowed_tools == ["run_python"]
    assert skill.context_mode == "inline"
    assert "# Alpha" in skill.body
    assert skill.source_path.name == "SKILL.md"


def test_malformed_frontmatter_is_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Good skill.
    _write_skill(
        tmp_path,
        "good",
        "name: good\ndescription: A working skill.",
        body="body",
    )
    # Broken: invalid YAML inside the frontmatter block.
    bad_dir = tmp_path / "broken"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text(
        "---\nname: broken\ndescription: [unclosed list\n---\nbody\n",
        encoding="utf-8",
    )
    # No frontmatter at all.
    nofm_dir = tmp_path / "nofm"
    nofm_dir.mkdir()
    (nofm_dir / "SKILL.md").write_text("# Just markdown\nNo frontmatter.\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_worker.skills.loader"):
        reg = load_skills_dir(tmp_path)
    assert reg.names() == ["good"], "good skill must still load even when siblings are malformed"
    warning_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "broken" in warning_text or "invalid" in warning_text.lower()
    assert "nofm" in warning_text or "no frontmatter" in warning_text.lower()


def test_index_summary_truncates_per_skill(tmp_path: Path) -> None:
    long_desc = "x" * 1_000
    _write_skill(tmp_path, "verbose", f"name: verbose\ndescription: {long_desc}")
    reg = load_skills_dir(tmp_path)
    summary = reg.index_summary(max_chars_per_skill=50)
    # The truncated line includes the name prefix; the description portion
    # must not exceed the cap.
    line = summary.splitlines()[0]
    _, _, desc_part = line.partition(": ")
    assert len(desc_part) <= 50
    # And the ellipsis marker proves it was actually truncated.
    assert desc_part.endswith("…")


def test_get_and_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, "one", "name: one\ndescription: One.")
    _write_skill(tmp_path, "two", "name: two\ndescription: Two.")
    reg = load_skills_dir(tmp_path)
    assert reg.get("one") is not None
    assert reg.get("two") is not None
    assert reg.get("three") is None
    assert set(reg.names()) == {"one", "two"}


def test_multi_skill_dir_loads_in_alphabetical_order(tmp_path: Path) -> None:
    _write_skill(tmp_path, "charlie", "name: charlie\ndescription: c.")
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: a.")
    _write_skill(tmp_path, "bravo", "name: bravo\ndescription: b.")
    reg = load_skills_dir(tmp_path)
    # Deterministic order matters for cache hits when this listing lands
    # in the system prompt.
    assert reg.names() == ["alpha", "bravo", "charlie"]


def test_when_to_use_list_and_string_both_parse(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "as_list",
        """
name: as_list
description: Has when_to_use as a YAML list.
when_to_use:
  - "trigger one"
  - "trigger two"
""",
    )
    _write_skill(
        tmp_path,
        "as_string",
        """
name: as_string
description: Has when_to_use as a single scalar string.
when_to_use: "Use when the user wants X"
""",
    )
    reg = load_skills_dir(tmp_path)
    assert reg.get("as_list").when_to_use == ["trigger one", "trigger two"]  # type: ignore[union-attr]
    assert reg.get("as_string").when_to_use == ["Use when the user wants X"]  # type: ignore[union-attr]


def test_symlinked_skill_md_is_followed(tmp_path: Path) -> None:
    # Create a real skill file outside the registry root, then symlink it in.
    external = tmp_path / "external.md"
    external.write_text(
        "---\nname: external\ndescription: Loaded via symlink.\n---\n\nbody\n",
        encoding="utf-8",
    )
    root = tmp_path / "skills_root"
    root.mkdir()
    skill_dir = root / "external"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").symlink_to(external)

    reg = load_skills_dir(root)
    assert reg.names() == ["external"]
    assert reg.get("external").description == "Loaded via symlink."  # type: ignore[union-attr]


def test_format_index_languages(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A.")
    reg = load_skills_dir(tmp_path)
    en = format_index_for_prompt(reg, language="en")
    zh = format_index_for_prompt(reg, language="zh")
    assert en.startswith("## Available skills")
    assert zh.startswith("## 可用技能")
    assert "- alpha: A." in en
    assert "- alpha: A." in zh


def test_explicit_registry_construction_sorts() -> None:
    skills = [
        Skill(name="zulu", description="z"),
        Skill(name="alpha", description="a"),
    ]
    reg = SkillRegistry(skills)
    assert reg.names() == ["alpha", "zulu"]
