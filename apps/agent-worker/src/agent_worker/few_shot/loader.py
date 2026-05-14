"""Few-shot library loader."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CompetitionFamily = Literal["mcm", "icm", "cumcm", "huashu"]

# Default index location: <repo_root>/data/mcm/index/winning_papers.jsonl.
# This file lives at apps/agent-worker/src/agent_worker/few_shot/loader.py, so
# repo root is 5 directories up (parents[5]).
_REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_INDEX_PATH = _REPO_ROOT / "data" / "mcm" / "index" / "winning_papers.jsonl"


@dataclass(frozen=True)
class Exemplar:
    """A single winning-paper snippet usable as a prompt few-shot."""

    paper_id: str
    year: int
    problem_letter: str  # 'A' .. 'F'
    competition_type: CompetitionFamily
    summary_text: str
    section_headings: tuple[str, ...]
    has_sensitivity_section: bool
    has_strengths_weaknesses_section: bool


class FewShotLibrary:
    """In-memory index of winning-paper exemplars, keyed by (family, letter).

    Loaded lazily from JSONL. Missing file → empty library, top_k returns [].
    """

    def __init__(self, exemplars: list[Exemplar]) -> None:
        self._exemplars = exemplars
        # Bucket by (family, letter) for fast lookup.
        self._buckets: dict[tuple[str, str], list[Exemplar]] = {}
        for ex in exemplars:
            key = (ex.competition_type, ex.problem_letter)
            self._buckets.setdefault(key, []).append(ex)
        # Sort each bucket: prefer those with sensitivity + S/W sections
        # (the very rules the Writer must satisfy), then by recency.
        for _key, bucket in self._buckets.items():
            bucket.sort(
                key=lambda e: (
                    -int(e.has_sensitivity_section),
                    -int(e.has_strengths_weaknesses_section),
                    -e.year,
                )
            )

    @classmethod
    def from_jsonl(cls, path: Path | None = None) -> FewShotLibrary:
        """Load from JSONL. Non-existent / unreadable file → empty library."""
        resolved = path or DEFAULT_INDEX_PATH
        # Allow override via env var so users can point at a custom index.
        if env_path := os.environ.get("MM_FEW_SHOT_INDEX"):
            resolved = Path(env_path)
        if not resolved.is_file():
            return cls([])
        rows: list[Exemplar] = []
        try:
            with resolved.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not (summary := obj.get("summary_text") or ""):
                        # Skip records that failed text extraction —
                        # they would just take prompt slots without value.
                        continue
                    rows.append(
                        Exemplar(
                            paper_id=str(obj.get("id", "")),
                            year=int(obj.get("year", 0)),
                            problem_letter=str(obj.get("problem_letter", "")).upper(),
                            competition_type=str(
                                obj.get("competition_type", "mcm")
                            ).lower(),  # type: ignore[arg-type]
                            summary_text=summary[:2000],
                            section_headings=tuple(
                                str(h) for h in obj.get("section_headings", [])
                            ),
                            has_sensitivity_section=bool(
                                obj.get("has_sensitivity_section", False)
                            ),
                            has_strengths_weaknesses_section=bool(
                                obj.get("has_strengths_weaknesses_section", False)
                            ),
                        )
                    )
        except OSError:
            return cls([])
        return cls(rows)

    def __len__(self) -> int:
        return len(self._exemplars)

    def top_k(
        self,
        competition_type: str,
        problem_letter: str | None,
        k: int = 3,
    ) -> list[Exemplar]:
        """Return up to k exemplars matching the family + (optional) letter.

        Matching strategy (each step falls through if it yields too few):
        1. Exact (family, letter) bucket.
        2. Same family, any letter — same competition culture/judging style.
        3. Cross-family fallback (mcm↔icm and cumcm↔huashu share rubrics).
        4. Anything in the library (last resort).
        """
        family = _normalize_family(competition_type)
        letter = (problem_letter or "").upper() or None

        picked: list[Exemplar] = []
        seen: set[str] = set()

        def _add(items: list[Exemplar]) -> None:
            for ex in items:
                if ex.paper_id in seen:
                    continue
                seen.add(ex.paper_id)
                picked.append(ex)
                if len(picked) >= k:
                    return

        if letter:
            _add(self._buckets.get((family, letter), []))
        if len(picked) < k:
            same_family = [
                e for e in self._exemplars if e.competition_type == family
            ]
            same_family.sort(
                key=lambda e: (
                    -int(e.has_sensitivity_section),
                    -int(e.has_strengths_weaknesses_section),
                    -e.year,
                )
            )
            _add(same_family)
        if len(picked) < k:
            sibling = _SIBLING_FAMILY.get(family)
            if sibling:
                sibs = [
                    e for e in self._exemplars if e.competition_type == sibling
                ]
                sibs.sort(
                    key=lambda e: (
                        -int(e.has_sensitivity_section),
                        -int(e.has_strengths_weaknesses_section),
                        -e.year,
                    )
                )
                _add(sibs)
        if len(picked) < k:
            rest = sorted(
                self._exemplars,
                key=lambda e: (
                    -int(e.has_sensitivity_section),
                    -int(e.has_strengths_weaknesses_section),
                    -e.year,
                ),
            )
            _add(rest)

        return picked[:k]


_SIBLING_FAMILY: dict[str, str] = {
    "mcm": "icm",
    "icm": "mcm",
    "cumcm": "huashu",
    "huashu": "cumcm",
}


def _normalize_family(competition_type: str) -> str:
    """Map raw competition_type strings to one of mcm/icm/cumcm/huashu."""
    s = (competition_type or "").lower()
    if "icm" in s:
        return "icm"
    if "mcm" in s:
        return "mcm"
    if "huashu" in s or "华数" in s:
        return "huashu"
    if "cumcm" in s or "国赛" in s:
        return "cumcm"
    return "mcm"


def format_writer_block(exemplars: list[Exemplar], language: str = "en") -> str:
    """Render exemplars as a prompt-injectable block.

    `language` controls headings:
    - "en" for MCM/ICM
    - "zh" for CUMCM/华数杯

    Empty input → empty string, so a missing index degrades silently.
    """
    if not exemplars:
        return ""
    if language == "zh":
        header = (
            "## 同题型获奖论文范本（仅供参考行文风格 + 章节结构，禁止照抄文字）\n"
            "以下是 dick20 语料库中同题型 (problem_letter 匹配优先) 历年获奖论文的摘要 / 节选。"
            "判官最看重的是：摘要里塞具体数字、敏感性章节定量、结论与开篇呼应。"
            "请观察并复用其结构与论证密度，但**禁止抄写其文字或数据**——你的数字必须来自 Coder 的本次实验。\n"
        )
        item_label = "范本"
    else:
        header = (
            "## Award-winning exemplars from prior years (style + structure reference only — do NOT copy text)\n"
            "Each block is the Summary section (and detected section headings) of a past Outstanding / Finalist paper "
            "on the same problem letter when available. Note how the abstract front-loads numerical findings, how the "
            "Sensitivity Analysis section is structured, and how Strengths/Weaknesses ties back to modeling choices. "
            "**You must NOT copy text or numbers from these exemplars.** Your own numbers must come from this run's "
            "Coder output.\n"
        )
        item_label = "Exemplar"
    parts = [header]
    for i, ex in enumerate(exemplars, 1):
        headings = ", ".join(ex.section_headings) if ex.section_headings else "(headings not detected)"
        flags = []
        if ex.has_sensitivity_section:
            flags.append("✓Sensitivity")
        if ex.has_strengths_weaknesses_section:
            flags.append("✓Strengths/Weaknesses")
        flag_str = (" [" + ", ".join(flags) + "]") if flags else ""
        parts.append(
            f"### {item_label} {i}: {ex.year} Problem {ex.problem_letter}"
            f" ({ex.competition_type.upper()}){flag_str}\n"
            f"Detected sections: {headings}\n\n"
            f"Summary excerpt:\n{ex.summary_text.strip()}\n"
        )
    return "\n".join(parts)


__all__ = [
    "CompetitionFamily",
    "DEFAULT_INDEX_PATH",
    "Exemplar",
    "FewShotLibrary",
    "format_writer_block",
]
