"""Tools used by the PaperEditor agent.

Each tool implements the `Tool` protocol: an async `execute(args, ctx)` that
mutates the shared `ToolContext` and returns a `ToolResult`. Tools are
intentionally side-effectful (writing paper.md, mutating notebook.ipynb,
executing kernel cells, calling the gateway exporter) — the agent loop is
the only thing that decides WHEN to call them; the tools themselves never
talk to the LLM.

The five-tool catalogue mirrors the most common fine-tune verbs we observed
during the M2/M3 award-mode rollout:

* `read_paper`        — inspect the current draft.
* `edit_section`      — replace one section's body markdown verbatim.
* `edit_constant`     — patch a `NAME = ...` assignment in the notebook AND
                        re-execute the affected cells in the persistent kernel.
* `run_cell`          — run arbitrary Python (regenerate a chart, sanity-check
                        a number, etc.); the cell is appended to notebook.ipynb.
* `regenerate_figure` — convenience wrapper around `run_cell` that verifies
                        `figures/<id>.png` actually landed on disk.
* `recompile_pdf`     — call the gateway export pipeline to rebuild paper.pdf.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ToolContext:
    """Shared mutable state threaded through every tool call.

    The agent loads paper.meta.json and notebook.ipynb once at the top of the
    loop into a `ToolContext`, and each tool reads/mutates the in-memory
    structures + persists them back to disk as needed. This avoids re-parsing
    a 50KB notebook JSON on every turn.
    """

    run_dir: Path
    paper_meta: dict[str, Any] = field(default_factory=dict)
    notebook: dict[str, Any] = field(default_factory=dict)
    # `Any` rather than `KernelSession` to keep this module importable from
    # tests that mock the kernel without spinning up jupyter_client.
    kernel: Any | None = None
    gateway: Any | None = None
    # `Any` to avoid a hard dep on EventEmitter for the tools themselves; the
    # agent loop is responsible for the high-level finetune.* event emissions.
    emitter: Any | None = None
    # Auto-incrementing index for cells the editor appends to notebook.ipynb.
    next_cell_index: int = 0


@dataclass
class ToolResult:
    """Uniform return shape so the agent loop can format LLM feedback consistently."""

    ok: bool
    summary: str
    detail: str | None = None
    error: str | None = None


class Tool(Protocol):
    """Static interface every editor tool implements."""

    name: str

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


# ---------------------------------------------------------------- helpers


def _write_paper_artifacts(ctx: ToolContext) -> None:
    """Persist paper.meta.json AND re-render paper.md from the in-memory meta.

    Both files share a single source of truth (the in-memory `paper_meta`
    dict). We rewrite the whole markdown rather than patching ranges so the
    file always matches the JSON exactly — the export pipeline (tectonic +
    pandoc) only reads paper.meta.json, but the live preview UI reads
    paper.md, so they MUST not drift.
    """
    meta_path = ctx.run_dir / "paper.meta.json"
    meta_path.write_text(
        json.dumps(ctx.paper_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paper_path = ctx.run_dir / "paper.md"
    paper_path.write_text(_render_paper_md(ctx.paper_meta), encoding="utf-8")


def _render_paper_md(meta: dict[str, Any]) -> str:
    """Re-render paper.md from paper_meta — mirrors pipeline._render_paper_markdown."""
    title = meta.get("title") or "Paper"
    abstract = meta.get("abstract") or ""
    parts: list[str] = [f"# {title}", "", "## Abstract", "", abstract]
    for sec in meta.get("sections", []) or []:
        parts.extend(["", f"## {sec.get('title', '')}", "", sec.get("body_markdown", "")])
    refs = meta.get("references") or []
    if refs:
        parts.extend(["", "## References", ""])
        for i, ref in enumerate(refs, start=1):
            parts.append(f"{i}. {ref}")
    return "\n".join(parts) + "\n"


def _find_section(meta: dict[str, Any], title: str) -> dict[str, Any] | None:
    """Exact-match lookup; we intentionally do NOT lowercase or strip — the LLM
    is told to match titles verbatim and silent fuzzy-matching has bitten us
    before (it once renamed "Sensitivity Analysis" to "sensitivity analysis"
    in the rendered PDF).
    """
    for sec in meta.get("sections", []) or []:
        if sec.get("title") == title:
            return sec
    return None


def _section_titles(meta: dict[str, Any]) -> list[str]:
    return [s.get("title", "") for s in meta.get("sections", []) or []]


# ---------------------------------------------------------------- tools


class ReadPaperTool:
    """Return paper section text — full structure by default, one body on demand.

    args:
      section_title: Optional[str]  # None -> return all section titles + 200-char snippets
    """

    name = "read_paper"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = args.get("section_title")
        meta = ctx.paper_meta
        if title is None:
            outline: list[dict[str, Any]] = [
                {"title": "<title>", "body_snippet": meta.get("title", "")},
                {
                    "title": "Abstract",
                    "body_snippet": (meta.get("abstract") or "")[:200],
                },
            ]
            for sec in meta.get("sections", []) or []:
                outline.append(
                    {
                        "title": sec.get("title", ""),
                        "body_snippet": (sec.get("body_markdown", "") or "")[:200],
                    }
                )
            return ToolResult(
                ok=True,
                summary=(
                    f"Paper has {len(meta.get('sections', []) or [])} sections + abstract."
                ),
                detail=json.dumps(outline, ensure_ascii=False, indent=2),
            )
        if title in ("Abstract", "abstract"):
            return ToolResult(
                ok=True,
                summary="Abstract body returned.",
                detail=meta.get("abstract") or "",
            )
        sec = _find_section(meta, title)
        if sec is None:
            return ToolResult(
                ok=False,
                summary=f"Unknown section_title {title!r}.",
                error=(
                    f"No section with title {title!r}. Known titles: "
                    f"{_section_titles(meta)}"
                ),
            )
        return ToolResult(
            ok=True,
            summary=f"Section {title!r} body returned ({len(sec.get('body_markdown', ''))} chars).",
            detail=sec.get("body_markdown", ""),
        )


class EditSectionTool:
    """Replace one section's body_markdown verbatim and re-persist paper artifacts.

    args:
      section_title: str
      new_body_md:   str
    """

    name = "edit_section"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = args.get("section_title")
        new_body = args.get("new_body_md")
        if not isinstance(title, str) or not title:
            return ToolResult(
                ok=False,
                summary="edit_section requires non-empty section_title.",
                error="missing section_title",
            )
        if not isinstance(new_body, str):
            return ToolResult(
                ok=False,
                summary="edit_section requires new_body_md as a string.",
                error="missing or non-string new_body_md",
            )
        # Abstract has its own top-level key in paper_meta — treat it as a
        # special case so users can ask "tighten the abstract" naturally.
        if title in ("Abstract", "abstract"):
            ctx.paper_meta["abstract"] = new_body
            _write_paper_artifacts(ctx)
            return ToolResult(
                ok=True,
                summary=f"Replaced abstract ({len(new_body)} chars).",
            )
        sec = _find_section(ctx.paper_meta, title)
        if sec is None:
            return ToolResult(
                ok=False,
                summary=f"Unknown section_title {title!r}.",
                error=(
                    f"No section with title {title!r}. Known titles: "
                    f"{_section_titles(ctx.paper_meta)}"
                ),
            )
        sec["body_markdown"] = new_body
        _write_paper_artifacts(ctx)
        return ToolResult(
            ok=True,
            summary=f"Replaced section {title!r} body ({len(new_body)} chars).",
        )


# Matches `NAME = <rhs>` at the start of a line (allowing leading whitespace).
# We intentionally capture only the bare assignment form — augmented forms
# (`NAME += 1`) and tuple unpacking (`A, B = ...`) are out of scope; the LLM
# is told to use `run_cell` for anything fancier.
_ASSIGN_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.+?)\s*$")


def _format_value(value: Any) -> str:
    """Render a Python literal for the RHS of an assignment.

    Strings get repr (handles quoting and escaping). Booleans go through
    `str()` so we emit `True`/`False`, not Python-repr-of-bool variants.
    Everything else (int, float, list, dict) round-trips through `repr`.
    """
    if isinstance(value, bool):  # noqa: SIM103 — order matters: bool is an int subclass
        return "True" if value else "False"
    if isinstance(value, str):
        return repr(value)
    return repr(value)


class EditConstantTool:
    """Patch a `NAME = ...` assignment in the notebook and re-execute affected cells.

    args:
      name:              str            (required) — exact identifier on the LHS
      value:             scalar         (required) — new RHS; rendered via repr
      rerun_cells_from:  Optional[int]  (default: index of the cell we edited)
    """

    name = "edit_constant"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(
                ok=False,
                summary="edit_constant requires a `name` identifier.",
                error="missing name",
            )
        if "value" not in args:
            return ToolResult(
                ok=False,
                summary="edit_constant requires a `value`.",
                error="missing value",
            )
        new_value_src = _format_value(args["value"])

        cells = ctx.notebook.get("cells", []) or []
        edited_cell_idx: int | None = None
        edited_line_preview: str | None = None
        for idx, cell in enumerate(cells):
            if cell.get("cell_type") != "code":
                continue
            source = cell.get("source", "")
            lines = source.splitlines(keepends=True) if isinstance(source, str) else list(source)
            for li, line in enumerate(lines):
                m = _ASSIGN_RE.match(line.rstrip("\n"))
                if m and m.group(2) == name:
                    indent = m.group(1)
                    newline_suffix = "\n" if line.endswith("\n") else ""
                    lines[li] = f"{indent}{name} = {new_value_src}{newline_suffix}"
                    edited_line_preview = lines[li].rstrip("\n")
                    break
            else:
                continue
            # Persist the patched source (we hit the inner `break` above).
            cell["source"] = "".join(lines)
            edited_cell_idx = idx
            break

        if edited_cell_idx is None:
            return ToolResult(
                ok=False,
                summary=f"No `{name} = ...` assignment found in notebook.",
                error=f"constant {name!r} not found",
            )

        # Persist the patched notebook before re-execution so users inspecting
        # notebook.ipynb mid-rerun see the new RHS.
        notebook_path = ctx.run_dir / "notebook.ipynb"
        notebook_path.write_text(
            json.dumps(ctx.notebook, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        rerun_from = args.get("rerun_cells_from")
        if rerun_from is None:
            rerun_from = edited_cell_idx
        try:
            rerun_from_i = int(rerun_from)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                summary="rerun_cells_from must be an integer.",
                error=f"bad rerun_cells_from {rerun_from!r}",
            )

        rerun_count = 0
        rerun_errors: list[str] = []
        if ctx.kernel is not None:
            for idx in range(rerun_from_i, len(cells)):
                cell = cells[idx]
                if cell.get("cell_type") != "code":
                    continue
                source = cell.get("source", "")
                if isinstance(source, list):
                    source = "".join(source)
                # Use the cell's original index so downstream debugging maps 1:1.
                result = await ctx.kernel.execute(
                    source, cell_index=idx, emitter=ctx.emitter
                )
                rerun_count += 1
                if getattr(result, "error", None):
                    rerun_errors.append(f"cell {idx}: {result.error}")

        summary_parts = [
            f"Patched {name} = {new_value_src} in cell {edited_cell_idx}.",
            f"Re-executed {rerun_count} cell(s) from index {rerun_from_i}.",
        ]
        if rerun_errors:
            summary_parts.append(f"{len(rerun_errors)} cell(s) errored on rerun.")
        return ToolResult(
            ok=not rerun_errors,
            summary=" ".join(summary_parts),
            detail=(
                "Edited line: "
                + (edited_line_preview or "")
                + ("\n" + "\n".join(rerun_errors) if rerun_errors else "")
            ),
            error="; ".join(rerun_errors) if rerun_errors else None,
        )


def _append_code_cell(ctx: ToolContext, code: str) -> int:
    """Append a fresh code cell to notebook.ipynb (in-memory) and bump the index."""
    cells = ctx.notebook.setdefault("cells", [])
    cell_idx = ctx.next_cell_index
    ctx.next_cell_index += 1
    cells.append(
        {
            "cell_type": "code",
            "execution_count": cell_idx + 1,
            "metadata": {"added_by": "paper_editor"},
            "outputs": [],
            "source": code,
        }
    )
    notebook_path = ctx.run_dir / "notebook.ipynb"
    notebook_path.write_text(
        json.dumps(ctx.notebook, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return cell_idx


class RunCellTool:
    """Execute arbitrary Python in the persistent kernel; append to notebook.

    args:
      code: str  (required)
    """

    name = "run_cell"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolResult(
                ok=False,
                summary="run_cell requires non-empty code.",
                error="missing code",
            )
        if ctx.kernel is None:
            return ToolResult(
                ok=False,
                summary="run_cell requires a live kernel.",
                error="no kernel session attached",
            )
        cell_idx = _append_code_cell(ctx, code)
        result = await ctx.kernel.execute(code, cell_index=cell_idx, emitter=ctx.emitter)
        err = getattr(result, "error", None)
        stdout = getattr(result, "stdout", "") or ""
        if err:
            return ToolResult(
                ok=False,
                summary=f"Cell {cell_idx} errored: {err.splitlines()[0]}",
                detail=stdout,
                error=err,
            )
        return ToolResult(
            ok=True,
            summary=f"Cell {cell_idx} executed cleanly ({len(stdout)} stdout chars).",
            detail=stdout[:2000] if stdout else None,
        )


class RegenerateFigureTool:
    """Run code AND verify that `figures/<figure_id>.png` was produced.

    args:
      figure_id: str  (required)
      code:      str  (required)
    """

    name = "regenerate_figure"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        figure_id = args.get("figure_id")
        code = args.get("code")
        if not isinstance(figure_id, str) or not figure_id:
            return ToolResult(
                ok=False,
                summary="regenerate_figure requires figure_id.",
                error="missing figure_id",
            )
        if not isinstance(code, str) or not code.strip():
            return ToolResult(
                ok=False,
                summary="regenerate_figure requires non-empty code.",
                error="missing code",
            )
        if ctx.kernel is None:
            return ToolResult(
                ok=False,
                summary="regenerate_figure requires a live kernel.",
                error="no kernel session attached",
            )

        png_rel = f"figures/{figure_id}.png"
        png_abs = ctx.run_dir / png_rel
        mtime_before = png_abs.stat().st_mtime if png_abs.exists() else 0.0

        cell_idx = _append_code_cell(ctx, code)
        result = await ctx.kernel.execute(code, cell_index=cell_idx, emitter=ctx.emitter)
        err = getattr(result, "error", None)
        if err:
            return ToolResult(
                ok=False,
                summary=f"Cell {cell_idx} errored before figure landed: {err.splitlines()[0]}",
                detail=getattr(result, "stdout", ""),
                error=err,
            )
        if not png_abs.is_file():
            return ToolResult(
                ok=False,
                summary=f"Cell ran but {png_rel} does not exist.",
                error=f"missing artifact {png_rel}",
            )
        mtime_after = png_abs.stat().st_mtime
        if mtime_after <= mtime_before:
            return ToolResult(
                ok=False,
                summary=f"{png_rel} exists but mtime did not advance — figure was NOT regenerated.",
                error=f"stale artifact {png_rel}",
            )
        return ToolResult(
            ok=True,
            summary=f"Regenerated {png_rel} (cell {cell_idx}).",
        )


class RecompilePdfTool:
    """Re-build paper.pdf by calling the gateway export pipeline.

    args: {} (none)
    """

    name = "recompile_pdf"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.gateway is None:
            return ToolResult(
                ok=False,
                summary="recompile_pdf requires a gateway client.",
                error="no gateway attached",
            )
        run_id = ctx.paper_meta.get("run_id")
        # Many callers pass run_id directly on the ctx via the gateway's
        # surrounding pipeline. We accept either path.
        if run_id is None:
            run_id = args.get("run_id")
        if run_id is None:
            run_id = getattr(ctx, "run_id", None)
        if run_id is None:
            # Fall back to inferring from run_dir name (uuid).
            run_id = ctx.run_dir.name
        try:
            pdf_bytes = await ctx.gateway.export_paper(run_id=run_id, format="pdf")
        except Exception as exc:  # noqa: BLE001 — surface upstream errors uniformly
            return ToolResult(
                ok=False,
                summary=f"Gateway export_paper failed: {exc!s}",
                error=str(exc),
            )
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            return ToolResult(
                ok=False,
                summary=f"Gateway returned non-PDF payload ({len(pdf_bytes)} bytes).",
                error="non-pdf payload",
            )
        pdf_path = ctx.run_dir / "paper.pdf"
        pdf_path.write_bytes(pdf_bytes)
        return ToolResult(
            ok=True,
            summary=f"paper.pdf rebuilt ({len(pdf_bytes)} bytes).",
        )


def build_tool_registry() -> dict[str, Tool]:
    """Map tool name -> instance. The agent uses this to dispatch directives."""
    instances: list[Tool] = [
        ReadPaperTool(),
        EditSectionTool(),
        EditConstantTool(),
        RunCellTool(),
        RegenerateFigureTool(),
        RecompilePdfTool(),
    ]
    return {t.name: t for t in instances}


__all__ = [
    "EditConstantTool",
    "EditSectionTool",
    "ReadPaperTool",
    "RecompilePdfTool",
    "RegenerateFigureTool",
    "RunCellTool",
    "Tool",
    "ToolContext",
    "ToolResult",
    "build_tool_registry",
]
