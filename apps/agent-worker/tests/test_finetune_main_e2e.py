"""End-to-end smoke for `run_finetune` — the finetune-job orchestrator.

Covers the wiring that ``test_paper_editor_agent.py`` deliberately skips:
that ``run_finetune`` (a) opens an EventEmitter pinned to the run's
events.jsonl, (b) constructs PaperEditorAgent with the right collaborators,
(c) drives one fine-tune turn against a canned LLM directive stream, and
(d) emits both ``finetune.session.start`` and ``finetune.session.done``
events. The internal turn-level loop is covered by the agent tests; here
we verify that the *integration seam* doesn't drift.

We monkeypatch ``GatewayClient`` and ``KernelSession`` constructors inside
``finetune_main`` so the test doesn't touch the real network or spawn a
Jupyter kernel. Redis is faked too — ``EventEmitter`` only calls a handful
of methods on it, so a minimal AsyncMock suffices.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agent_worker import finetune_main
from agent_worker.config import Settings


def _seed_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "figures").mkdir()
    meta = {
        "title": "End-to-End Test Paper",
        "abstract": "Original abstract.",
        "competition_type": "mcm",
        "problem_text": "stub",
        "sections": [
            {"title": "Summary", "body_markdown": "Original summary."},
        ],
        "references": [],
        "figures": [],
    }
    (run_dir / "paper.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "paper.md").write_text(
        "# Paper\n\n## Abstract\n\nOriginal abstract.\n", encoding="utf-8"
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


class _FakeGateway:
    """Streams a single canned `done` directive so PaperEditor exits its
    loop after one turn without doing any edits. That keeps the test small
    while still exercising the open / close / emit lifecycle.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.export_paper = AsyncMock(return_value=b"%PDF-1.7\nfake")

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        self.calls += 1
        yield json.dumps(
            {
                "reasoning": "nothing to change",
                "tool": "read_paper",
                "args": {"section_title": "Summary"},
                "done": True,
                "summary": "No changes needed.",
            }
        )

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_redis() -> AsyncMock:
    """Minimal AsyncMock that satisfies the EventEmitter call surface.

    EventEmitter uses ``redis.xadd(stream, fields, maxlen=N, approximate=True)``;
    AsyncMock by default returns coroutine mocks for any awaited attribute,
    which is fine for our purposes — we just need it to not raise.
    """
    return AsyncMock()


async def test_run_finetune_emits_session_start_and_done(
    tmp_path: Path,
    fake_redis: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    session_id = uuid4()

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / str(run_id)).mkdir()
    # Re-seed under the run-id-specific path that finetune_main expects.
    seeded = _seed_run_dir(tmp_path)
    for f in seeded.iterdir():
        if f.is_dir():
            (runs_dir / str(run_id) / f.name).mkdir(exist_ok=True)
        else:
            (runs_dir / str(run_id) / f.name).write_bytes(f.read_bytes())

    # Settings reads RUNS_DIR via env-var alias; setting the kwarg directly
    # is shadowed unless populate_by_name is on (it isn't). Route through
    # the env var so the Pydantic loader picks it up.
    monkeypatch.setenv("RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("REDIS_URL", "redis://stub")
    monkeypatch.setenv("GATEWAY_HTTP", "http://stub")
    cfg = Settings()

    fake_gw = _FakeGateway()
    # The kernel is only used for PaperEditor's `run_code` tool, which the
    # canned directive doesn't exercise. AsyncMock is enough.
    fake_kernel = AsyncMock()

    def _gateway_factory(*_args: Any, **_kw: Any) -> _FakeGateway:
        return fake_gw

    def _kernel_factory(*_args: Any, **_kw: Any) -> AsyncMock:
        return fake_kernel

    monkeypatch.setattr(finetune_main, "GatewayClient", _gateway_factory)
    monkeypatch.setattr(finetune_main, "KernelSession", _kernel_factory)

    await finetune_main.run_finetune(
        redis=fake_redis,
        cfg=cfg,
        run_id=run_id,
        session_id=session_id,
        user_message="ensure abstract is concise",
    )

    # events.jsonl is the forensic log. It MUST contain both session
    # bookends — the start banner and the terminal done. If either is
    # missing, the frontend's finetune store hangs on a spinner.
    events_path = runs_dir / str(run_id) / "events.jsonl"
    assert events_path.exists(), "events.jsonl missing — emitter never opened"
    raw_lines = events_path.read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["kind"] for line in raw_lines]

    assert "finetune.session.start" in kinds, kinds
    assert "finetune.session.done" in kinds, kinds
    # session.error must not fire on a clean run.
    assert "finetune.session.error" not in kinds

    # Gateway must be closed exactly once (finally block in run_finetune).
    assert fake_gw.closed is True
    # The agent did one LLM turn against our canned `done` directive.
    assert fake_gw.calls >= 1


async def test_run_finetune_bails_when_run_dir_missing(
    tmp_path: Path,
    fake_redis: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finetune request against a deleted/unknown run must not crash;
    log + return is the contract (the consumer loop must keep draining).
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setenv("RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("REDIS_URL", "redis://stub")
    monkeypatch.setenv("GATEWAY_HTTP", "http://stub")
    cfg = Settings()

    fake_gw = _FakeGateway()
    monkeypatch.setattr(finetune_main, "GatewayClient", lambda *a, **k: fake_gw)
    monkeypatch.setattr(finetune_main, "KernelSession", lambda *a, **k: AsyncMock())

    missing_run_id = uuid4()
    await finetune_main.run_finetune(
        redis=fake_redis,
        cfg=cfg,
        run_id=missing_run_id,
        session_id=uuid4(),
        user_message="anything",
    )

    # No events.jsonl ever materialised because the emitter never opened.
    assert not (runs_dir / str(missing_run_id)).exists()
    # GatewayClient was never even instantiated — finetune_main bails
    # before constructing it.
    assert fake_gw.calls == 0
    assert fake_gw.closed is False
