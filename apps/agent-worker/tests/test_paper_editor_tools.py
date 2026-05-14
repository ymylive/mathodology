"""Unit tests for paper-editor tools.

Every test builds a fake `run_dir` in `tmp_path` with paper.meta.json,
paper.md, and a tiny notebook.ipynb, then exercises one tool in isolation.
The kernel + gateway are mocked with `AsyncMock` so no kernel subprocess is
spawned and no real HTTP call is made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agent_worker.editor_tools import (
    EditConstantTool,
    EditSectionTool,
    ReadPaperTool,
    RecompilePdfTool,
    RegenerateFigureTool,
    RunCellTool,
    ToolContext,
)

PAPER_META: dict[str, Any] = {
    "title": "On the Stability of Lamprey Sex Ratios",
    "abstract": "We model sea-lamprey sex determination as a function of resource availability...",
    "competition_type": "mcm",
    "problem_text": "Some problem text.",
    "sections": [
        {"title": "Summary", "body_markdown": "Original summary body."},
        {
            "title": "Sensitivity Analysis",
            "body_markdown": "We perturb K_R by +/-20% and observe...",
        },
        {
            "title": "Strengths and Weaknesses",
            "body_markdown": "Strengths: ... Weaknesses: ...",
        },
    ],
    "references": ["Ref 1", "Ref 2"],
    "figures": [],
}


NOTEBOOK: dict[str, Any] = {
    "cells": [
        {
            "cell_type": "code",
            "execution_count": 1,
            "metadata": {},
            "outputs": [],
            "source": "K_R = 3000\nK_S = 4000\nprint('constants loaded')",
        },
        {
            "cell_type": "code",
            "execution_count": 2,
            "metadata": {},
            "outputs": [],
            "source": "result = K_R / K_S\nprint(result)",
        },
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _make_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "figures").mkdir()
    (run_dir / "paper.meta.json").write_text(json.dumps(PAPER_META, ensure_ascii=False), encoding="utf-8")
    (run_dir / "paper.md").write_text("# title\n\n## Abstract\n\nbody\n", encoding="utf-8")
    (run_dir / "notebook.ipynb").write_text(json.dumps(NOTEBOOK, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _make_ctx(
    tmp_path: Path,
    *,
    kernel: Any | None = None,
    gateway: Any | None = None,
) -> ToolContext:
    run_dir = _make_run_dir(tmp_path)
    return ToolContext(
        run_dir=run_dir,
        paper_meta=json.loads(json.dumps(PAPER_META)),  # deep copy
        notebook=json.loads(json.dumps(NOTEBOOK)),
        kernel=kernel,
        gateway=gateway,
        emitter=None,
        next_cell_index=10,
    )


async def test_read_paper_full(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await ReadPaperTool().execute({}, ctx)
    assert result.ok
    # detail must be a JSON list containing every section title
    parsed = json.loads(result.detail or "[]")
    titles = [entry["title"] for entry in parsed]
    assert "Summary" in titles
    assert "Sensitivity Analysis" in titles
    assert "Strengths and Weaknesses" in titles


async def test_read_paper_one_section(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await ReadPaperTool().execute({"section_title": "Summary"}, ctx)
    assert result.ok
    assert result.detail == "Original summary body."


async def test_read_paper_unknown_section_errors(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await ReadPaperTool().execute({"section_title": "Nonexistent"}, ctx)
    assert not result.ok
    assert "Nonexistent" in (result.error or "")


async def test_edit_section_replaces_body_md_and_writes_files(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    new_body = "Brand-new summary tightened to 500 words."
    result = await EditSectionTool().execute(
        {"section_title": "Summary", "new_body_md": new_body}, ctx
    )
    assert result.ok
    # In-memory mutation
    summary_sec = next(
        s for s in ctx.paper_meta["sections"] if s["title"] == "Summary"
    )
    assert summary_sec["body_markdown"] == new_body
    # Disk persistence
    on_disk = json.loads((ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8"))
    assert any(
        s["title"] == "Summary" and s["body_markdown"] == new_body
        for s in on_disk["sections"]
    )
    rendered_md = (ctx.run_dir / "paper.md").read_text(encoding="utf-8")
    assert new_body in rendered_md
    assert "## Summary" in rendered_md


async def test_edit_section_unknown_title_errors(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await EditSectionTool().execute(
        {"section_title": "Bogus", "new_body_md": "..."}, ctx
    )
    assert not result.ok
    assert "Bogus" in (result.error or "")


async def test_edit_section_abstract(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = await EditSectionTool().execute(
        {"section_title": "Abstract", "new_body_md": "Tightened abstract."}, ctx
    )
    assert result.ok
    assert ctx.paper_meta["abstract"] == "Tightened abstract."
    on_disk = json.loads((ctx.run_dir / "paper.meta.json").read_text(encoding="utf-8"))
    assert on_disk["abstract"] == "Tightened abstract."


async def test_edit_constant_modifies_notebook_source(tmp_path: Path) -> None:
    kernel = AsyncMock()
    # Mock kernel.execute returning an object with .error=None and .stdout=''.
    kernel.execute.return_value = type("R", (), {"error": None, "stdout": "ok"})()
    ctx = _make_ctx(tmp_path, kernel=kernel)
    result = await EditConstantTool().execute({"name": "K_R", "value": 5000}, ctx)
    assert result.ok, result.error
    patched_src = ctx.notebook["cells"][0]["source"]
    assert "K_R = 5000" in patched_src
    # Persisted to disk too
    on_disk = json.loads((ctx.run_dir / "notebook.ipynb").read_text(encoding="utf-8"))
    assert "K_R = 5000" in on_disk["cells"][0]["source"]
    # Both cells from index 0 should have been re-executed (cell 0 + cell 1).
    assert kernel.execute.await_count == 2


async def test_edit_constant_missing_name_errors(tmp_path: Path) -> None:
    kernel = AsyncMock()
    ctx = _make_ctx(tmp_path, kernel=kernel)
    result = await EditConstantTool().execute(
        {"name": "DOES_NOT_EXIST", "value": 99}, ctx
    )
    assert not result.ok
    assert "DOES_NOT_EXIST" in (result.error or "")
    kernel.execute.assert_not_called()


async def test_edit_constant_requires_value(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, kernel=AsyncMock())
    result = await EditConstantTool().execute({"name": "K_R"}, ctx)
    assert not result.ok
    assert "value" in (result.error or "").lower()


async def test_run_cell_executes_via_kernel_and_appends_to_notebook(
    tmp_path: Path,
) -> None:
    kernel = AsyncMock()
    kernel.execute.return_value = type(
        "R", (), {"error": None, "stdout": "hello\n"}
    )()
    ctx = _make_ctx(tmp_path, kernel=kernel)
    cells_before = len(ctx.notebook["cells"])
    result = await RunCellTool().execute({"code": "print('hello')"}, ctx)
    assert result.ok
    assert len(ctx.notebook["cells"]) == cells_before + 1
    assert ctx.notebook["cells"][-1]["source"] == "print('hello')"
    kernel.execute.assert_awaited_once()
    # Persisted
    on_disk = json.loads((ctx.run_dir / "notebook.ipynb").read_text(encoding="utf-8"))
    assert on_disk["cells"][-1]["source"] == "print('hello')"


async def test_run_cell_propagates_kernel_error(tmp_path: Path) -> None:
    kernel = AsyncMock()
    kernel.execute.return_value = type(
        "R", (), {"error": "NameError: foo", "stdout": ""}
    )()
    ctx = _make_ctx(tmp_path, kernel=kernel)
    result = await RunCellTool().execute({"code": "foo"}, ctx)
    assert not result.ok
    assert "NameError" in (result.error or "")


async def test_regenerate_figure_verifies_png_landed(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, kernel=AsyncMock())

    fig_path = ctx.run_dir / "figures" / "tornado.png"

    # Simulate the kernel actually writing the PNG when execute() runs.
    async def fake_execute(source: str, cell_index: int, emitter: Any) -> Any:
        fig_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return type("R", (), {"error": None, "stdout": ""})()

    ctx.kernel.execute.side_effect = fake_execute  # type: ignore[union-attr]

    result = await RegenerateFigureTool().execute(
        {"figure_id": "tornado", "code": "plt.savefig('figures/tornado.png')"},
        ctx,
    )
    assert result.ok, result.error
    assert fig_path.is_file()


async def test_regenerate_figure_errors_when_png_missing(tmp_path: Path) -> None:
    kernel = AsyncMock()
    kernel.execute.return_value = type("R", (), {"error": None, "stdout": ""})()
    ctx = _make_ctx(tmp_path, kernel=kernel)
    result = await RegenerateFigureTool().execute(
        {"figure_id": "doesnotexist", "code": "x = 1"}, ctx
    )
    assert not result.ok
    assert "doesnotexist" in (result.error or "")


async def test_recompile_pdf_calls_gateway_export_paper(tmp_path: Path) -> None:
    gateway = AsyncMock()
    gateway.export_paper.return_value = b"%PDF-1.7\n...rest of pdf bytes..."
    ctx = _make_ctx(tmp_path, gateway=gateway)
    result = await RecompilePdfTool().execute({}, ctx)
    assert result.ok, result.error
    gateway.export_paper.assert_awaited_once()
    pdf_on_disk = (ctx.run_dir / "paper.pdf").read_bytes()
    assert pdf_on_disk.startswith(b"%PDF")


async def test_recompile_pdf_handles_gateway_500(tmp_path: Path) -> None:
    import httpx

    gateway = AsyncMock()
    gateway.export_paper.side_effect = httpx.HTTPError("upstream 500")
    ctx = _make_ctx(tmp_path, gateway=gateway)
    result = await RecompilePdfTool().execute({}, ctx)
    assert not result.ok
    assert "upstream 500" in (result.error or "")
    assert not (ctx.run_dir / "paper.pdf").is_file()


async def test_recompile_pdf_rejects_non_pdf_payload(tmp_path: Path) -> None:
    gateway = AsyncMock()
    gateway.export_paper.return_value = b"<html>oops</html>"
    ctx = _make_ctx(tmp_path, gateway=gateway)
    result = await RecompilePdfTool().execute({}, ctx)
    assert not result.ok
    assert (result.error or "").startswith("non-pdf")


@pytest.fixture
def ctx_with_kernel(tmp_path: Path) -> ToolContext:
    """Convenience fixture for tests that need a generic ctx + mocked kernel."""
    kernel = AsyncMock()
    kernel.execute.return_value = type("R", (), {"error": None, "stdout": ""})()
    return _make_ctx(tmp_path, kernel=kernel)


async def test_edit_constant_with_string_value(ctx_with_kernel: ToolContext) -> None:
    # Verify string values get repr'd (preserving the quotes in the source).
    ctx_with_kernel.notebook["cells"][0]["source"] = "MODE = 'baseline'\nprint(MODE)"
    result = await EditConstantTool().execute(
        {"name": "MODE", "value": "adaptive"}, ctx_with_kernel
    )
    assert result.ok
    patched_src = ctx_with_kernel.notebook["cells"][0]["source"]
    assert "MODE = 'adaptive'" in patched_src
