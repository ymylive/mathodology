"""Tests for the surgical_edit tool added in round 8.

The tool mirrors Anthropic's Edit-tool discipline:
  * exact-string `old_text` match (NO regex)
  * uniqueness across the searched scope; ambiguity surfaces a clear error
  * optional `replace_all` to override uniqueness
  * clear, actionable errors so the agent can retry without a full rewrite

These tests cover:
  * happy path (unique match, single replacement)
  * no-match (clear error, paper unchanged on disk)
  * duplicate match without `replace_all` (error with location count)
  * duplicate match WITH `replace_all` (every occurrence rewritten)
  * scoping via `section_title`
  * empty / non-string args
  * paper.md gets re-rendered from paper.meta.json after a write
  * the agent loop emits `finetune.tool_call` + `finetune.tool_result`
    events when dispatching to surgical_edit (event-stream contract).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agent_worker.agents import PaperEditorAgent
from agent_worker.editor_tools import SurgicalEditTool, ToolContext

PAPER_META: dict[str, Any] = {
    "title": "On the Stability of Lamprey Sex Ratios",
    "abstract": "We model sea-lamprey sex determination as a function of resource availability. The K_R parameter dominates.",
    "competition_type": "mcm",
    "problem_text": "Some problem text.",
    "sections": [
        {
            "title": "Summary",
            "body_markdown": (
                "The lamprey population is sensitive to K_R. "
                "Reducing K_R by 20% halves the male fraction."
            ),
        },
        {
            "title": "Sensitivity Analysis",
            "body_markdown": (
                "We perturb K_R by +/-20% and observe the male fraction shift. "
                "K_R dominates the tornado plot."
            ),
        },
        {
            "title": "Strengths and Weaknesses",
            "body_markdown": "Strengths: parsimonious. Weaknesses: ignores migration.",
        },
    ],
    "references": ["Ref 1", "Ref 2"],
    "figures": [],
}


def _make_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "figures").mkdir()
    (run_dir / "paper.meta.json").write_text(
        json.dumps(PAPER_META, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "paper.md").write_text(
        "# title\n\n## Abstract\n\nbody\n", encoding="utf-8"
    )
    (run_dir / "notebook.ipynb").write_text(
        json.dumps(
            {
                "cells": [],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _make_ctx(tmp_path: Path) -> ToolContext:
    run_dir = _make_run_dir(tmp_path)
    return ToolContext(
        run_dir=run_dir,
        paper_meta=json.loads(json.dumps(PAPER_META)),
        notebook={"cells": []},
        kernel=None,
        gateway=None,
        emitter=None,
        next_cell_index=0,
    )


# --------------------------------------------------------------- happy paths


async def test_surgical_edit_unique_match_replaces_once(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    # "parsimonious" appears exactly once (in Strengths and Weaknesses).
    result = await SurgicalEditTool().execute(
        {"old_text": "parsimonious", "new_text": "minimal"}, ctx
    )
    assert result.ok, result.error
    strengths = next(
        s for s in ctx.paper_meta["sections"] if s["title"] == "Strengths and Weaknesses"
    )
    assert "minimal" in strengths["body_markdown"]
    assert "parsimonious" not in strengths["body_markdown"]
    # paper.meta.json was persisted to disk
    on_disk = json.loads(
        (ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8")
    )
    on_disk_strengths = next(
        s for s in on_disk["sections"] if s["title"] == "Strengths and Weaknesses"
    )
    assert "minimal" in on_disk_strengths["body_markdown"]
    # paper.md re-rendered too
    md = (ctx.run_dir / "paper.md").read_text(encoding="utf-8")
    assert "minimal" in md
    # diff window in detail
    assert result.detail is not None
    assert "- parsimonious" in result.detail
    assert "+ minimal" in result.detail


async def test_surgical_edit_can_edit_abstract(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    # "resource availability" appears once (in abstract).
    result = await SurgicalEditTool().execute(
        {
            "old_text": "resource availability",
            "new_text": "habitat carrying capacity",
        },
        ctx,
    )
    assert result.ok, result.error
    assert "habitat carrying capacity" in ctx.paper_meta["abstract"]
    assert "resource availability" not in ctx.paper_meta["abstract"]


# ------------------------------------------------------------------ no match


async def test_surgical_edit_no_match_returns_error_and_does_not_write(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    before = (ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8")
    result = await SurgicalEditTool().execute(
        {"old_text": "totally-not-in-the-paper-xyz", "new_text": "anything"}, ctx
    )
    assert not result.ok
    assert "not found" in (result.error or "").lower()
    # No write should have happened.
    after = (ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8")
    assert before == after


# -------------------------------------------------------------- duplicate match


async def test_surgical_edit_duplicate_match_without_replace_all_errors(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    # "K_R" appears in abstract + Summary + Sensitivity Analysis (multiple times).
    before = (ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8")
    result = await SurgicalEditTool().execute(
        {"old_text": "K_R", "new_text": "K_recruit"}, ctx
    )
    assert not result.ok
    err = (result.error or "").lower()
    assert "non-unique" in err or "occurrence" in err
    # The error should mention at least one container so the agent knows where.
    assert "summary" in err or "sensitivity" in err or "abstract" in err
    # No write should have happened.
    after = (ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8")
    assert before == after


async def test_surgical_edit_duplicate_with_replace_all_replaces_everything(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    # Count K_R occurrences before.
    rendered_before = (
        ctx.paper_meta["abstract"]
        + " ".join(s["body_markdown"] for s in ctx.paper_meta["sections"])
    )
    occurrences = rendered_before.count("K_R")
    assert occurrences > 1  # sanity: ambiguous before replace_all
    result = await SurgicalEditTool().execute(
        {"old_text": "K_R", "new_text": "K_recruit", "replace_all": True}, ctx
    )
    assert result.ok, result.error
    # No K_R should remain.
    rendered_after = (
        ctx.paper_meta["abstract"]
        + " ".join(s["body_markdown"] for s in ctx.paper_meta["sections"])
    )
    assert "K_R" not in rendered_after
    assert rendered_after.count("K_recruit") == occurrences
    # paper.md mirrors paper.meta.json
    md = (ctx.run_dir / "paper.md").read_text(encoding="utf-8")
    assert "K_recruit" in md
    assert "K_R" not in md


# -------------------------------------------------------------- section_title scoping


async def test_surgical_edit_section_title_narrows_scope(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    # "K_R" is ambiguous globally, but inside Strengths and Weaknesses it
    # doesn't appear at all — so a Summary-scoped edit should be possible
    # if the anchor is unique within Summary.
    # The phrase "Reducing K_R by 20%" is unique to Summary.
    result = await SurgicalEditTool().execute(
        {
            "old_text": "Reducing K_R by 20%",
            "new_text": "Lowering K_R by one fifth",
            "section_title": "Summary",
        },
        ctx,
    )
    assert result.ok, result.error
    summary_sec = next(s for s in ctx.paper_meta["sections"] if s["title"] == "Summary")
    assert "Lowering K_R by one fifth" in summary_sec["body_markdown"]


async def test_surgical_edit_unknown_section_title_errors(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await SurgicalEditTool().execute(
        {
            "old_text": "K_R",
            "new_text": "K_recruit",
            "section_title": "Made Up Section",
        },
        ctx,
    )
    assert not result.ok
    assert "Made Up Section" in (result.error or "")


# ------------------------------------------------------------------- validation


async def test_surgical_edit_empty_old_text_errors(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await SurgicalEditTool().execute(
        {"old_text": "", "new_text": "anything"}, ctx
    )
    assert not result.ok
    assert "old_text" in (result.error or "").lower()


async def test_surgical_edit_missing_new_text_errors(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await SurgicalEditTool().execute({"old_text": "parsimonious"}, ctx)
    assert not result.ok
    assert "new_text" in (result.error or "").lower()


async def test_surgical_edit_noop_when_old_equals_new(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await SurgicalEditTool().execute(
        {"old_text": "parsimonious", "new_text": "parsimonious"}, ctx
    )
    assert not result.ok
    assert "noop" in (result.summary or "").lower() or "equals" in (
        result.error or ""
    ).lower()


# ----------------------------------------------------- agent-loop event emission


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict[str, Any], str | None]] = []

    async def emit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> None:
        self.events.append((kind, payload or {}, agent))


class _FakeGateway:
    def __init__(self, directives: list[str]) -> None:
        self._directives = directives
        self.calls = 0
        self.export_paper = AsyncMock(return_value=b"%PDF-1.7\nfake")

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        idx = self.calls
        self.calls += 1
        if idx >= len(self._directives):
            idx = len(self._directives) - 1
        yield self._directives[idx]

    async def close(self) -> None:
        pass


def _seed_run_dir_for_agent(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "figures").mkdir()
    (run_dir / "paper.meta.json").write_text(
        json.dumps(PAPER_META, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "paper.md").write_text(
        "# title\n\n## Abstract\n\n...\n", encoding="utf-8"
    )
    (run_dir / "notebook.ipynb").write_text(
        json.dumps(
            {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        ),
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture
def kernel_mock() -> AsyncMock:
    kernel = AsyncMock()
    kernel.execute.return_value = type(
        "R", (), {"error": None, "stdout": ""}
    )()
    return kernel


async def test_agent_dispatches_surgical_edit_and_emits_events(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir_for_agent(tmp_path)
    directives = [
        json.dumps(
            {
                "reasoning": "fix typo with a surgical edit",
                "tool": "surgical_edit",
                "args": {
                    "old_text": "parsimonious",
                    "new_text": "minimal",
                },
                "done": True,
                "summary": "Replaced 'parsimonious' with 'minimal'.",
            }
        )
    ]
    gateway = _FakeGateway(directives)
    emitter = _FakeEmitter()
    agent = PaperEditorAgent(
        gateway=gateway,  # type: ignore[arg-type]
        emitter=emitter,  # type: ignore[arg-type]
        kernel=kernel_mock,
        run_dir=run_dir,
    )
    summary = await agent.fine_tune(
        "Replace 'parsimonious' with 'minimal'.", run_dir, max_iterations=4
    )
    assert "minimal" in summary.lower() or "parsimonious" in summary

    kinds = [e[0] for e in emitter.events]
    assert "finetune.tool_call" in kinds
    assert "finetune.tool_result" in kinds

    tool_call = next(e for e in emitter.events if e[0] == "finetune.tool_call")
    assert tool_call[1]["tool"] == "surgical_edit"
    assert tool_call[1]["args"]["old_text"] == "parsimonious"

    tool_result = next(e for e in emitter.events if e[0] == "finetune.tool_result")
    assert tool_result[1]["tool"] == "surgical_edit"
    assert tool_result[1]["ok"] is True

    # Verify the on-disk paper actually got the edit.
    on_disk = json.loads((run_dir / "paper.meta.json").read_text(encoding="utf-8"))
    strengths = next(
        s for s in on_disk["sections"] if s["title"] == "Strengths and Weaknesses"
    )
    assert "minimal" in strengths["body_markdown"]
    assert "parsimonious" not in strengths["body_markdown"]
