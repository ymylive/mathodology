"""Tests for FewShotLibrary: graceful degradation + correct ranking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_worker.few_shot import (
    Exemplar,
    FewShotLibrary,
    format_writer_block,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_missing_file_yields_empty_library(tmp_path: Path) -> None:
    lib = FewShotLibrary.from_jsonl(tmp_path / "does_not_exist.jsonl")
    assert len(lib) == 0
    assert lib.top_k("mcm", "A") == []


def test_records_without_summary_are_skipped(tmp_path: Path) -> None:
    idx = tmp_path / "winning.jsonl"
    _write_jsonl(
        idx,
        [
            {
                "id": "2024_A_001",
                "year": 2024,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "",  # extraction failure
                "section_headings": [],
            },
            {
                "id": "2024_A_002",
                "year": 2024,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "Real summary with numbers like 3.2% and 17 kg.",
                "section_headings": ["Introduction", "Model"],
            },
        ],
    )
    lib = FewShotLibrary.from_jsonl(idx)
    assert len(lib) == 1


def test_top_k_prefers_sensitivity_then_recency(tmp_path: Path) -> None:
    idx = tmp_path / "winning.jsonl"
    _write_jsonl(
        idx,
        [
            {
                "id": "old_no_sens",
                "year": 2010,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "old paper",
                "section_headings": [],
                "has_sensitivity_section": False,
                "has_strengths_weaknesses_section": False,
            },
            {
                "id": "new_with_sens",
                "year": 2023,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "new paper with sens",
                "section_headings": ["Sensitivity"],
                "has_sensitivity_section": True,
                "has_strengths_weaknesses_section": True,
            },
            {
                "id": "newest_no_sens",
                "year": 2024,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "newest no sens",
                "section_headings": [],
                "has_sensitivity_section": False,
                "has_strengths_weaknesses_section": False,
            },
        ],
    )
    lib = FewShotLibrary.from_jsonl(idx)
    top = lib.top_k("mcm", "A", k=3)
    assert [e.paper_id for e in top] == ["new_with_sens", "newest_no_sens", "old_no_sens"]


def test_top_k_falls_through_letter_then_sibling_family(tmp_path: Path) -> None:
    idx = tmp_path / "winning.jsonl"
    _write_jsonl(
        idx,
        [
            # MCM A — wrong letter for our query, same family
            {
                "id": "mcm_A",
                "year": 2024,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "MCM A",
                "section_headings": [],
            },
            # ICM D — sibling family (mcm↔icm), used as 3rd-tier fallback
            {
                "id": "icm_D",
                "year": 2024,
                "problem_letter": "D",
                "competition_type": "icm",
                "summary_text": "ICM D",
                "section_headings": [],
            },
        ],
    )
    lib = FewShotLibrary.from_jsonl(idx)
    # Query: MCM problem B (no exact match). Should pull from same family first.
    top = lib.top_k("mcm", "B", k=2)
    ids = [e.paper_id for e in top]
    assert ids[0] == "mcm_A"
    # Second item should fall through to sibling family
    assert "icm_D" in ids


def test_normalize_family_handles_cn_aliases(tmp_path: Path) -> None:
    idx = tmp_path / "winning.jsonl"
    _write_jsonl(
        idx,
        [
            {
                "id": "cumcm_A",
                "year": 2023,
                "problem_letter": "A",
                "competition_type": "cumcm",
                "summary_text": "国赛 A",
                "section_headings": [],
            }
        ],
    )
    lib = FewShotLibrary.from_jsonl(idx)
    # raw "国赛" should map to cumcm family
    assert lib.top_k("国赛", "A", k=1)[0].paper_id == "cumcm_A"
    # "huashu" should fall through to sibling cumcm
    assert lib.top_k("huashu", "A", k=1)[0].paper_id == "cumcm_A"


def test_format_writer_block_empty_returns_empty_string() -> None:
    assert format_writer_block([]) == ""


def test_format_writer_block_renders_zh_header_for_cumcm() -> None:
    ex = Exemplar(
        paper_id="x",
        year=2022,
        problem_letter="A",
        competition_type="cumcm",
        summary_text="摘要……",
        section_headings=("引言", "模型"),
        has_sensitivity_section=True,
        has_strengths_weaknesses_section=False,
    )
    block = format_writer_block([ex], language="zh")
    assert "同题型获奖论文范本" in block
    assert "✓Sensitivity" in block
    assert "禁止抄写其文字或数据" in block


def test_format_writer_block_renders_en_header_for_mcm() -> None:
    ex = Exemplar(
        paper_id="y",
        year=2024,
        problem_letter="C",
        competition_type="mcm",
        summary_text="Summary with 3.2% accuracy.",
        section_headings=("Introduction", "Sensitivity Analysis"),
        has_sensitivity_section=True,
        has_strengths_weaknesses_section=True,
    )
    block = format_writer_block([ex], language="en")
    assert "Award-winning exemplars" in block
    assert "must NOT copy" in block
    assert "✓Sensitivity" in block
    assert "✓Strengths/Weaknesses" in block


def test_env_override_index_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idx = tmp_path / "custom.jsonl"
    _write_jsonl(
        idx,
        [
            {
                "id": "env_test",
                "year": 2024,
                "problem_letter": "A",
                "competition_type": "mcm",
                "summary_text": "via env",
                "section_headings": [],
            }
        ],
    )
    monkeypatch.setenv("MM_FEW_SHOT_INDEX", str(idx))
    lib = FewShotLibrary.from_jsonl()  # no explicit path
    assert len(lib) == 1
    assert lib.top_k("mcm", "A", k=1)[0].paper_id == "env_test"
