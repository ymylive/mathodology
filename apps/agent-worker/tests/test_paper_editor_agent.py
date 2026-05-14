"""PaperEditorAgent loop test — canned LLM directives, mocked kernel + gateway.

The agent's only external collaborator is the LLM stream, so we drive the
loop by feeding it a list of canned JSON directives via a `_FakeGateway`.
The kernel and gateway exporter are mocked. All file I/O hits `tmp_path`.
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
from agent_worker.agents.base import AgentError

PAPER_META: dict[str, Any] = {
    "title": "Test Paper",
    "abstract": "Original abstract.",
    "competition_type": "mcm",
    "problem_text": "stub",
    "sections": [
        {"title": "Summary", "body_markdown": "Original summary."},
        {"title": "Sensitivity Analysis", "body_markdown": "Original sensitivity."},
    ],
    "references": [],
    "figures": [],
}


def _seed_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "figures").mkdir()
    (run_dir / "paper.meta.json").write_text(
        json.dumps(PAPER_META, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "paper.md").write_text("# Test\n\n## Abstract\n\n...\n", encoding="utf-8")
    (run_dir / "notebook.ipynb").write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "execution_count": 1,
                        "metadata": {},
                        "outputs": [],
                        "source": "x = 1",
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


class _FakeEmitter:
    """Record emit() calls; mimic the EventEmitter surface area used by the agent."""

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
    """Yields canned JSON strings from `stream_completion`; tracks call count.

    Also stubs `export_paper` so the recompile_pdf tool path can be exercised.
    """

    def __init__(self, directives: list[str]) -> None:
        self._directives = directives
        self.calls = 0
        self.export_paper = AsyncMock(return_value=b"%PDF-1.7\nfake pdf bytes")

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        idx = self.calls
        self.calls += 1
        if idx >= len(self._directives):
            # Cycle (mimic "model keeps looping" failure mode).
            idx = len(self._directives) - 1
        yield self._directives[idx]

    async def close(self) -> None:
        pass


@pytest.fixture
def kernel_mock() -> AsyncMock:
    kernel = AsyncMock()
    kernel.execute.return_value = type(
        "R", (), {"error": None, "stdout": "ran ok"}
    )()
    return kernel


async def test_agent_dispatches_read_then_edit_then_done(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir(tmp_path)
    directives = [
        # Turn 1: read the Summary section
        json.dumps(
            {
                "reasoning": "inspect current summary",
                "tool": "read_paper",
                "args": {"section_title": "Summary"},
                "done": False,
                "summary": None,
            }
        ),
        # Turn 2: replace it
        json.dumps(
            {
                "reasoning": "tighten",
                "tool": "edit_section",
                "args": {
                    "section_title": "Summary",
                    "new_body_md": "Tightened summary.",
                },
                "done": False,
                "summary": None,
            }
        ),
        # Turn 3: done
        json.dumps(
            {
                "reasoning": "satisfied",
                "tool": "read_paper",
                "args": {"section_title": "Summary"},
                "done": True,
                "summary": "Tightened the summary section.",
            }
        ),
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
        "Tighten the summary to 500 words.", run_dir, max_iterations=8
    )
    assert summary == "Tightened the summary section."
    # paper.meta.json was updated on disk by the edit_section call.
    on_disk = json.loads((run_dir / "paper.meta.json").read_text(encoding="utf-8"))
    body = next(s["body_markdown"] for s in on_disk["sections"] if s["title"] == "Summary")
    assert body == "Tightened summary."


async def test_agent_respects_max_iterations(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir(tmp_path)
    # Always-not-done directive — drives the loop to the cap.
    loop_directive = json.dumps(
        {
            "reasoning": "loop forever",
            "tool": "read_paper",
            "args": {},
            "done": False,
            "summary": None,
        }
    )
    gateway = _FakeGateway([loop_directive] * 20)
    emitter = _FakeEmitter()
    agent = PaperEditorAgent(
        gateway=gateway,  # type: ignore[arg-type]
        emitter=emitter,  # type: ignore[arg-type]
        kernel=kernel_mock,
        run_dir=run_dir,
    )
    summary = await agent.fine_tune("noop", run_dir, max_iterations=3)
    assert "iteration limit" in summary.lower()
    # Three turns -> three stream_completion calls.
    assert gateway.calls == 3


async def test_agent_errors_on_invalid_tool_name(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir(tmp_path)
    # `tool` not in the Literal set — Pydantic raises ValidationError; the
    # agent's parse-retry path kicks in once, then raises AgentError.
    bad_directive = json.dumps(
        {
            "reasoning": "wrong",
            "tool": "delete_everything",
            "args": {},
            "done": False,
            "summary": None,
        }
    )
    gateway = _FakeGateway([bad_directive, bad_directive])
    emitter = _FakeEmitter()
    agent = PaperEditorAgent(
        gateway=gateway,  # type: ignore[arg-type]
        emitter=emitter,  # type: ignore[arg-type]
        kernel=kernel_mock,
        run_dir=run_dir,
    )
    with pytest.raises(AgentError):
        await agent.fine_tune("noop", run_dir, max_iterations=3)


async def test_agent_emits_finetune_tool_call_and_tool_result_events(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir(tmp_path)
    directives = [
        json.dumps(
            {
                "reasoning": "read",
                "tool": "read_paper",
                "args": {},
                "done": True,
                "summary": "Done.",
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
    await agent.fine_tune("inspect", run_dir, max_iterations=8)
    kinds = [e[0] for e in emitter.events]
    assert "stage.start" in kinds
    assert "finetune.tool_call" in kinds
    assert "finetune.tool_result" in kinds
    assert "stage.done" in kinds
    # tool_call payload has the tool name + args echoed
    tool_call = next(e for e in emitter.events if e[0] == "finetune.tool_call")
    assert tool_call[1]["tool"] == "read_paper"


async def test_agent_returns_summary_on_done(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir(tmp_path)
    directives = [
        json.dumps(
            {
                "reasoning": "trivial",
                "tool": "read_paper",
                "args": {},
                "done": True,
                "summary": "All good — nothing to change.",
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
    summary = await agent.fine_tune("just check", run_dir, max_iterations=8)
    assert summary == "All good — nothing to change."


async def test_agent_recompile_pdf_writes_paper_pdf(
    tmp_path: Path, kernel_mock: AsyncMock
) -> None:
    run_dir = _seed_run_dir(tmp_path)
    directives = [
        json.dumps(
            {
                "reasoning": "rebuild pdf",
                "tool": "recompile_pdf",
                "args": {},
                "done": True,
                "summary": "PDF rebuilt.",
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
    summary = await agent.fine_tune("rebuild", run_dir, max_iterations=8)
    assert summary == "PDF rebuilt."
    assert (run_dir / "paper.pdf").read_bytes().startswith(b"%PDF")
    gateway.export_paper.assert_awaited_once()
