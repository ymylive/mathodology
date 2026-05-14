#!/usr/bin/env python3
"""Build few-shot exemplar index from MCM/ICM winning-paper PDFs in dick20.

Outputs:
  - data/mcm/index/winning_papers.jsonl   one record per winning paper
  - data/mcm/index/problems.jsonl         one record per problem statement
  - data/mcm/index/STATS.md               counts + extraction failures

Pure heuristic extraction (no LLM). Designed for ~502 PDFs in dick20.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pdfplumber

ROOT = Path("/Users/cornna/project/math_agent/data/mcm/repos/dick20")
INDEX_DIR = Path("/Users/cornna/project/math_agent/data/mcm/index")
INDEX_DIR.mkdir(parents=True, exist_ok=True)

WINNING_OUT = INDEX_DIR / "winning_papers.jsonl"
PROBLEMS_OUT = INDEX_DIR / "problems.jsonl"
STATS_OUT = INDEX_DIR / "STATS.md"

# Problem PDFs we want to skip from the winning-papers index.
PROBLEM_FILENAME_RE = re.compile(
    r"(?:MCM|ICM)[-_ ]?(?:Problem|2\d{3})|Problem[-_ ]?[A-F][_.]|"
    r"Judges?_Commentary|Press|Results|Addendum|Contest_AI_Policy|SubProcess",
    re.IGNORECASE,
)
# Letter subdir names we recognise.
LETTER_DIR_RE = re.compile(r"^(?:MCM|ICM)?20\d{2}?([A-F])$|^([A-F])$|^([A-F])题\d+篇$")
# 2018 zh style: "A题5篇"
# 2019 zh style: "MCM2019A", "ICM2019D"

PROBLEM_DIR_NAMES = {
    "problems",
    "2018_MCM-ICM_Problems",
    "2019_MCM-ICM_Problems",
    "Results",
    "其他奖项",
}
# Year directory regex: "2024美赛特等奖"
YEAR_DIR_RE = re.compile(r"^(20\d{2})美赛特等奖$")

# Letter inference from filename for flat-year layouts.
FILENAME_LETTER_RE = re.compile(r"^([A-F])(?:[-_类]|\d)", re.IGNORECASE)
# Control number inference (5-7 digits anywhere; pick longest match early)
CONTROL_NUMBER_RE = re.compile(r"\b(\d{4,7})\b")

# Heading-related regexes.
SUMMARY_PAT = re.compile(
    r"(?im)^[ \t]*(?:Executive\s+)?(?:Summary(?:\s*Sheet)?|Abstract)\s*[:\n]"
)
HEADING_LINE_RE = re.compile(
    r"^\s*((?:[0-9]+\.)?[0-9]+)(?:\s+|\.\s*)"
    r"([A-Z][A-Za-z][^\n]{2,80})\s*$"
)
SENSITIVITY_RE = re.compile(r"sensitivity\s+analys", re.IGNORECASE)
STRENGTHS_RE = re.compile(
    r"strengths?\s*(?:&|and|/)\s*weakness|strengths?\s*and\s*weakness",
    re.IGNORECASE,
)
NUMBERED_REF_RE = re.compile(r"\[\d{1,3}\]")
AUTHOR_YEAR_RE = re.compile(r"\(([A-Z][a-zA-Z\-']+(?:\s+et\s+al\.?)?,?\s*\d{4}[a-z]?)\)")

LETTERS = {"A", "B", "C", "D", "E", "F"}


def comp_type(letter: str) -> str:
    return "mcm" if letter in {"A", "B"} else "icm"


def infer_letter(name: str, parent: str) -> Optional[str]:
    """Try parent-dir then filename to detect problem letter."""
    parent = parent.strip()
    # parent like 'A', 'B', 'C', 'D', 'E', 'F'
    if parent in LETTERS:
        return parent
    # zh: 'A题5篇'
    m = re.match(r"^([A-F])题\d+篇$", parent)
    if m:
        return m.group(1)
    # 2019: 'MCM2019A', 'ICM2019D'
    m = re.match(r"^(?:MCM|ICM)20\d{2}([A-F])$", parent)
    if m:
        return m.group(1)
    # filename starts like 'A-6749' or 'A76082' or 'A类-O奖--55069.pdf'
    m = FILENAME_LETTER_RE.match(name)
    if m:
        return m.group(1).upper()
    return None


def infer_control_number(name: str) -> str:
    """Find a control-number-looking integer in filename."""
    # Strip extension
    base = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    # Try patterns like '2425397', 'A-6749-Outstanding', 'A76082-...', 'A类-O奖--55069'
    nums = re.findall(r"\d{4,7}", base)
    if not nums:
        return ""
    # Pick the longest number (control numbers are typically 4-7 digits;
    # 6-7 digits for modern years, 3-5 for older)
    nums.sort(key=lambda s: (-len(s), -int(s)))
    return nums[0]


def looks_like_problem_pdf(name: str) -> bool:
    """Heuristic: is this a problem-statement PDF rather than a winning paper?"""
    return bool(PROBLEM_FILENAME_RE.search(name))


def find_summary(text: str) -> str:
    """Locate Summary/Abstract section in extracted text. Returns up to 2000 chars."""
    if not text:
        return ""
    m = SUMMARY_PAT.search(text)
    snippet = ""
    if m:
        start = m.end()
        snippet = text[start : start + 2500].strip()
    else:
        # Fallback 1: look for the word "Summary" mid-line (e.g. "Summary Sheet").
        idx = text.lower().find("summary sheet")
        if idx >= 0:
            after = text[idx:].split("\n", 1)
            snippet = after[1].strip() if len(after) > 1 else ""
        else:
            idx = text.lower().find("abstract")
            if idx >= 0:
                snippet = text[idx + len("abstract") :].strip()

    # Fallback 2: older papers may not have an Abstract — grab first
    # substantial paragraph after an "Introduction" heading.
    if not snippet or len(snippet) < 80:
        intro = re.search(
            r"(?im)^\s*(?:I\.\s*|1\s+|1\.\s*)?Introduction\b\s*\n", text
        )
        if intro:
            tail = text[intro.end() : intro.end() + 3000]
            snippet = tail.strip()

    if not snippet:
        return ""

    # Trim to first "Introduction"/"Contents"/"Keywords" boundary if reached
    for boundary in [
        re.compile(r"\n\s*(?:1\s+|1\.\s*)?(?:Introduction|Contents)\b", re.IGNORECASE),
        re.compile(r"\n\s*Keywords?\s*[:：]", re.IGNORECASE),
    ]:
        bm = boundary.search(snippet)
        if bm and bm.start() > 200:
            snippet = snippet[: bm.start()].strip()
            break
    # Normalize whitespace.
    snippet = re.sub(r"[ \t]+", " ", snippet)
    snippet = re.sub(r"\n{3,}", "\n\n", snippet)
    snippet = snippet.strip()
    # Reject obviously useless snippets (just dotted-leaders from TOC).
    if snippet.count(".") > len(snippet) * 0.3:
        return ""
    return snippet[:2000].strip()


def find_headings(text: str) -> list[str]:
    """Detect numbered top-level headings (e.g. '1 Introduction', '3 Model')."""
    if not text:
        return []
    headings: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        line = line.strip()
        # Skip lines that look like TOC entries (dotted leaders, trailing page #).
        if "..." in line or re.search(r"\.{4,}\s*\d+$", line):
            continue
        m = HEADING_LINE_RE.match(line)
        if not m:
            continue
        num, title = m.group(1), m.group(2).strip()
        # Only top-level (single number) headings, e.g. '1', '2', not '2.1'.
        if "." in num:
            continue
        # Strip trailing page numbers e.g. "Introduction 5"
        title = re.sub(r"\s+\d{1,3}$", "", title)
        if title.endswith("."):
            title = title[:-1]
        if len(title) < 3 or len(title) > 70:
            continue
        # de-dup on lowercased title
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        headings.append((num, title))
    # Cap at 20.
    return [t for _, t in headings[:20]]


def count_refs(text: str) -> int:
    if not text:
        return 0
    n1 = len(NUMBERED_REF_RE.findall(text))
    n2 = len(AUTHOR_YEAR_RE.findall(text))
    return n1 + n2


def extract_text(pdf_path: Path, max_pages: int = 60) -> tuple[int, str, str]:
    """Return (n_pages, head_text, full_text_capped).

    head_text = first 3 pages (for summary/title)
    full_text_capped = first max_pages pages joined (for headings/sensitivity/refs)
    """
    with pdfplumber.open(str(pdf_path)) as pdf:
        n = len(pdf.pages)
        head_parts: list[str] = []
        full_parts: list[str] = []
        for i, page in enumerate(pdf.pages[:max_pages]):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            full_parts.append(t)
            if i < 3:
                head_parts.append(t)
        head = "\n".join(head_parts)
        full = "\n".join(full_parts)
    return n, head, full


_TITLE_BLOCKLIST_RE = re.compile(
    r"^(Team|For\s+office|Problem|MCM|ICM|20\d{2}|T\d|F\d|\d{4,7}|_+|Page\b|"
    r"Contents\b|Table\s+of|Summary\b|Abstract\b|February|January|March|April|"
    r"May|June|July|August|September|October|November|December|"
    r"Mathematical\s+Contest|Group\s*#)",
    re.IGNORECASE,
)


def guess_title(head_text: str) -> str:
    """Heuristic title: line above 'Summary'/'Abstract' or the first all-caps-ish line."""
    if not head_text:
        return ""
    lines = [ln.strip() for ln in head_text.split("\n")]
    # Look for line right above Abstract/Summary
    for i, ln in enumerate(lines):
        if re.match(r"^(?:Executive\s+)?(?:Summary(?:\s*Sheet)?|Abstract)\s*$", ln, re.IGNORECASE):
            # Look back up to 8 lines for a meaty title
            for j in range(i - 1, max(0, i - 9), -1):
                cand = lines[j].strip()
                if 8 < len(cand) < 120 and not _TITLE_BLOCKLIST_RE.match(cand):
                    if not cand.endswith("________________"):
                        return cand
            break
    # Fallback: first line longer than 10 chars not matching boilerplate
    for ln in lines[:30]:
        if 10 < len(ln) < 120 and not _TITLE_BLOCKLIST_RE.match(ln):
            return ln
    return ""


def process_winning_paper(
    pdf_path: str, year: int, parent: str
) -> tuple[Optional[dict], Optional[str]]:
    """Extract a single winning paper record. Returns (record, error)."""
    p = Path(pdf_path)
    name = p.name
    letter = infer_letter(name, parent)
    cn = infer_control_number(name)
    try:
        n_pages, head, full = extract_text(p)
    except Exception as e:
        return None, f"{pdf_path}: extract failed: {e}"

    # Prefer head (first 3 pages) when it contains a Summary/Abstract heading;
    # otherwise scan a larger window of full text for fallback strategies.
    summary_source = head if SUMMARY_PAT.search(head or "") else full[:12000]
    summary = find_summary(summary_source)
    headings = find_headings(full)
    title = guess_title(head)
    rid = f"{year}_{letter or 'X'}_{cn or 'unknown'}"
    record = {
        "id": rid,
        "year": year,
        "problem_letter": letter,
        "competition_type": comp_type(letter) if letter else None,
        "path": str(p),
        "pages": n_pages,
        "title": title,
        "summary_text": summary,
        "section_headings": headings,
        "has_sensitivity_section": bool(SENSITIVITY_RE.search(full)),
        "has_strengths_weaknesses_section": bool(STRENGTHS_RE.search(full)),
        "ref_count_estimate": count_refs(full),
    }
    return record, None


def process_problem_pdf(
    pdf_path: str, year: int, letter: Optional[str]
) -> tuple[Optional[dict], Optional[str]]:
    p = Path(pdf_path)
    try:
        with pdfplumber.open(str(p)) as pdf:
            n_pages = len(pdf.pages)
            parts = []
            for page in pdf.pages[:8]:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    parts.append("")
            text = "\n".join(parts).strip()
    except Exception as e:
        return None, f"{pdf_path}: extract failed: {e}"

    # Look up sibling data files
    data_files: list[str] = []
    siblings = list(p.parent.iterdir())
    for s in siblings:
        if s.is_file() and s.suffix.lower() in {".csv", ".xlsx", ".zip", ".xls"}:
            data_files.append(str(s))
    record = {
        "year": year,
        "problem_letter": letter,
        "competition_type": comp_type(letter) if letter else None,
        "path": str(p),
        "pages": n_pages,
        "problem_text": text[:8000],
        "data_files": sorted(data_files),
    }
    return record, None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_files() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, Optional[str]]]]:
    """Walk dick20 corpus and return (winning_paper_tasks, problem_tasks)."""
    winning: list[tuple[str, int, str]] = []  # (path, year, parent_name)
    problems: list[tuple[str, int, Optional[str]]] = []  # (path, year, letter)

    for year_dir in sorted(ROOT.iterdir()):
        if not year_dir.is_dir():
            continue
        m = YEAR_DIR_RE.match(year_dir.name)
        if not m:
            continue
        year = int(m.group(1))

        for root, dirs, files in os.walk(year_dir):
            rel_root = Path(root)
            parent_name = rel_root.name
            # Decide if this dir is a problem dir
            is_problem_dir = parent_name in PROBLEM_DIR_NAMES
            is_other_award_dir = parent_name == "其他奖项"
            # Skip 其他奖项: per README those are not Outstanding/Finalist papers
            # we want for few-shot. We'll keep them anyway since user said
            # "winning papers" broadly. README says we just need O+F so skip.
            if is_other_award_dir:
                # Skip these papers for the index per spec
                # (they're Meritorious/Honorable, not our target)
                dirs[:] = []
                continue

            for fname in files:
                if not fname.lower().endswith(".pdf"):
                    continue
                full = str(rel_root / fname)

                if is_problem_dir:
                    # Skip judges commentaries, addenda, AI policy, sub-process docs
                    if re.search(
                        r"Judges?_Commentary|Commentary|Addendum|Contest_AI_Policy|"
                        r"SubProcess|Results|Press",
                        fname,
                        re.IGNORECASE,
                    ):
                        continue
                    # try to find problem letter
                    # filename patterns:
                    #   2024_MCM_Problem_A_FINAL.pdf
                    #   2024_ICM_Problem_D_FINAL.pdf
                    #   2018_ICM_Problem_D.pdf
                    #   2010_MCM_Problem_A.pdf  (in Results/)
                    #   2011-Problem-A.pdf
                    mlet = re.search(
                        r"Problem[_\- ]?([A-F])(?:[_\- .]|$)", fname, re.IGNORECASE
                    )
                    if mlet:
                        problems.append((full, year, mlet.group(1).upper()))
                    # else skip: it's a results / commentary / press file
                    continue

                # Otherwise: candidate winning paper
                if looks_like_problem_pdf(fname):
                    # Skip judge triage guides / commentaries / press releases etc.
                    if re.search(
                        r"Triage|Judges?_Commentary|Commentary|Press|Results|"
                        r"Addendum|AI_Policy|SubProcess|Tips",
                        fname,
                        re.IGNORECASE,
                    ):
                        continue
                    # Edge case: in flat-year dir, problem-statement PDFs sometimes appear at top level
                    mlet = re.search(
                        r"Problem[_\- ]?([A-F])(?:[_\- .]|$)", fname, re.IGNORECASE
                    )
                    if mlet:
                        problems.append((full, year, mlet.group(1).upper()))
                    continue

                winning.append((full, year, parent_name))

    return winning, problems


# ---------------------------------------------------------------------------
# Workers (top-level for ProcessPoolExecutor pickling)
# ---------------------------------------------------------------------------

def _worker_winning(args):
    pdf_path, year, parent = args
    try:
        return process_winning_paper(pdf_path, year, parent)
    except Exception as e:
        return None, f"{pdf_path}: worker exception: {e}\n{traceback.format_exc()}"


def _worker_problem(args):
    pdf_path, year, letter = args
    try:
        return process_problem_pdf(pdf_path, year, letter)
    except Exception as e:
        return None, f"{pdf_path}: worker exception: {e}\n{traceback.format_exc()}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    winning_tasks, problem_tasks = discover_files()
    print(
        f"Discovered {len(winning_tasks)} winning-paper PDFs, "
        f"{len(problem_tasks)} problem PDFs",
        flush=True,
    )

    winning_records: list[dict] = []
    winning_errors: list[str] = []

    problem_records: list[dict] = []
    problem_errors: list[str] = []

    # Use a process pool for parallel PDF parsing
    max_workers = max(2, (os.cpu_count() or 4) - 1)
    print(f"Using {max_workers} workers", flush=True)

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_worker_winning, t): t for t in winning_tasks}
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            rec, err = fut.result()
            done += 1
            if err:
                winning_errors.append(err)
            if rec:
                winning_records.append(rec)
            if done % 50 == 0 or done == total:
                print(
                    f"  winning: {done}/{total} (ok={len(winning_records)}, err={len(winning_errors)})",
                    flush=True,
                )

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_worker_problem, t): t for t in problem_tasks}
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            rec, err = fut.result()
            done += 1
            if err:
                problem_errors.append(err)
            if rec:
                problem_records.append(rec)
            if done % 20 == 0 or done == total:
                print(
                    f"  problems: {done}/{total} (ok={len(problem_records)}, err={len(problem_errors)})",
                    flush=True,
                )

    # Sort outputs for determinism
    winning_records.sort(key=lambda r: (r.get("year", 0), r.get("problem_letter") or "Z", r.get("id") or ""))
    problem_records.sort(key=lambda r: (r.get("year", 0), r.get("problem_letter") or "Z"))

    with WINNING_OUT.open("w", encoding="utf-8") as f:
        for r in winning_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with PROBLEMS_OUT.open("w", encoding="utf-8") as f:
        for r in problem_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Stats
    by_year_total: Counter = Counter()
    by_year_with_summary: Counter = Counter()
    by_letter: Counter = Counter()
    sens_count = 0
    sw_count = 0
    title_count = 0
    no_letter = 0
    summary_empty: list[str] = []
    for r in winning_records:
        y = r["year"]
        by_year_total[y] += 1
        if r["summary_text"]:
            by_year_with_summary[y] += 1
        else:
            summary_empty.append(r["path"])
        let = r.get("problem_letter") or "?"
        by_letter[let] += 1
        if let == "?":
            no_letter += 1
        if r.get("has_sensitivity_section"):
            sens_count += 1
        if r.get("has_strengths_weaknesses_section"):
            sw_count += 1
        if r.get("title"):
            title_count += 1

    n_w = len(winning_records)
    n_p = len(problem_records)
    pct_summary = 100.0 * sum(by_year_with_summary.values()) / n_w if n_w else 0.0
    elapsed = time.time() - t0

    # Build markdown stats
    lines: list[str] = []
    lines.append("# MCM/ICM dick20 corpus index — STATS")
    lines.append("")
    lines.append(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"- Elapsed: {elapsed:.1f}s  ")
    lines.append(f"- Source root: `{ROOT}`  ")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- Winning papers indexed: **{n_w}**  ")
    lines.append(f"- Winning papers with non-empty summary: **{sum(by_year_with_summary.values())}** ({pct_summary:.1f}%)  ")
    lines.append(f"- Winning papers with title: **{title_count}**  ")
    lines.append(f"- Winning papers with sensitivity section: **{sens_count}**  ")
    lines.append(f"- Winning papers with strengths/weaknesses section: **{sw_count}**  ")
    lines.append(f"- Winning papers with no detectable problem letter: **{no_letter}**  ")
    lines.append(f"- Problem PDFs indexed: **{n_p}**  ")
    lines.append("")
    lines.append("## Papers by year")
    lines.append("")
    lines.append("| Year | Total | With summary | % |")
    lines.append("|------|-------|--------------|---|")
    for y in sorted(by_year_total):
        tot = by_year_total[y]
        wsum = by_year_with_summary[y]
        pct = 100.0 * wsum / tot if tot else 0
        lines.append(f"| {y} | {tot} | {wsum} | {pct:.0f}% |")
    lines.append("")
    lines.append("## Papers by problem letter")
    lines.append("")
    lines.append("| Letter | Type | Count |")
    lines.append("|--------|------|-------|")
    for let in sorted(by_letter):
        ct = comp_type(let) if let in LETTERS else "?"
        lines.append(f"| {let} | {ct} | {by_letter[let]} |")
    lines.append("")
    lines.append("## Problem statements by year")
    lines.append("")
    p_by_year: Counter = Counter()
    p_by_letter: Counter = Counter()
    for r in problem_records:
        p_by_year[r["year"]] += 1
        p_by_letter[r.get("problem_letter") or "?"] += 1
    lines.append("| Year | Count |")
    lines.append("|------|-------|")
    for y in sorted(p_by_year):
        lines.append(f"| {y} | {p_by_year[y]} |")
    lines.append("")
    lines.append("## Problem statements by letter")
    lines.append("")
    lines.append("| Letter | Count |")
    lines.append("|--------|-------|")
    for let in sorted(p_by_letter):
        lines.append(f"| {let} | {p_by_letter[let]} |")
    lines.append("")
    lines.append(f"## Extraction failures ({len(winning_errors)} winning, {len(problem_errors)} problems)")
    lines.append("")
    if winning_errors:
        lines.append("### Winning-paper failures")
        for e in winning_errors[:40]:
            lines.append(f"- {e}")
        if len(winning_errors) > 40:
            lines.append(f"- ... and {len(winning_errors) - 40} more")
        lines.append("")
    if problem_errors:
        lines.append("### Problem-PDF failures")
        for e in problem_errors[:20]:
            lines.append(f"- {e}")
        lines.append("")
    lines.append(f"## Papers with empty summary_text ({len(summary_empty)})")
    lines.append("")
    for path in summary_empty[:40]:
        lines.append(f"- {path}")
    if len(summary_empty) > 40:
        lines.append(f"- ... and {len(summary_empty) - 40} more")
    lines.append("")
    lines.append("## Outputs")
    lines.append("")
    lines.append(f"- `{WINNING_OUT}`")
    lines.append(f"- `{PROBLEMS_OUT}`")
    lines.append("")

    STATS_OUT.write_text("\n".join(lines), encoding="utf-8")

    # Print final summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Winning papers indexed: {n_w}")
    print(f"  with non-empty summary_text: {sum(by_year_with_summary.values())} ({pct_summary:.1f}%)")
    print(f"  with sensitivity section: {sens_count}")
    print(f"  with strengths/weaknesses section: {sw_count}")
    print(f"Problem PDFs indexed: {n_p}")
    print()
    print("By year (papers / with_summary):")
    for y in sorted(by_year_total):
        print(f"  {y}: {by_year_total[y]:4d}  ({by_year_with_summary[y]:4d} w/summary)")
    print()
    print("By letter:")
    for let in sorted(by_letter):
        print(f"  {let}: {by_letter[let]}")
    print()
    print(f"Failures: {len(winning_errors)} winning, {len(problem_errors)} problems")
    print(f"Elapsed: {elapsed:.1f}s")
    print()
    print(f"Wrote: {WINNING_OUT}")
    print(f"Wrote: {PROBLEMS_OUT}")
    print(f"Wrote: {STATS_OUT}")


if __name__ == "__main__":
    main()
