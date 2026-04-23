"""`KernelSession` — lazy Jupyter kernel lifecycle for one run.

Each `run_id` gets its own kernel subprocess, working directory
(`<runs_dir>/<run_id>/`), and `figures/` subdir. `execute()` runs one code cell,
collects iopub messages until the kernel returns to idle, emits `kernel.stdout`
and `kernel.figure` events via the shared `EventEmitter`, and returns a
`CellExecution` record. `write_notebook()` serialises the whole run to
`notebook.ipynb` using `nbformat`.

Implementation notes
--------------------
* Kernel startup is deferred to `start()` so that constructing a session is
  cheap and safe to do before an agent is ready to run code.
* A module-style counter on the instance (`_figure_index`) makes figure
  filenames globally unique across cells within a session; one session per run
  so the counter resets implicitly.
* Each `execute()` is wrapped in `asyncio.wait_for(..., timeout=60)`. On
  timeout we interrupt the kernel and mark the cell as errored instead of
  bubbling out — the agent loop can still proceed or stop.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import nbformat
from jupyter_client import AsyncKernelManager
from mm_contracts import CellExecution

from agent_worker._chart_helpers import HELPER_SOURCE as _CHART_HELPERS_SRC

if TYPE_CHECKING:
    from agent_worker.events import EventEmitter


CELL_EXECUTION_TIMEOUT_S = 60
KERNEL_READY_TIMEOUT_S = 30

# Bootstrap source injected once per kernel, right after `start()`. Sets a
# deterministic matplotlib style with a Chinese-friendly font fallback chain
# and high savefig DPI so every figure is publication-grade. The chain lists
# macOS/Windows CJK fonts first and falls back to DejaVu Sans if none are
# installed — matplotlib skips missing families silently.
_MPL_BOOTSTRAP_SRC = (
    """
import logging
# The CJK font fallback chain below lists 4 candidates. On any single host
# only one will exist, and matplotlib logs "Font family 'X' not found" at
# INFO level for every miss, every figure — 3 spammy lines per plot. The
# render is still correct (it uses the first found family) so we silence the
# font_manager logger up to ERROR.
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = [
    "Songti SC", "PingFang SC", "Microsoft YaHei", "SimSun", "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.figsize"] = (6.4, 4.0)
plt.rcParams["figure.dpi"] = 120
plt.rcParams["savefig.dpi"] = 300
plt.rcParams["savefig.bbox"] = "tight"
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.alpha"] = 0.25
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.spines.right"] = False
"""
    # Helpers (`styled_figure`, `save_figure`, `annotate_peak`) are defined
    # in `agent_worker._chart_helpers` and inlined here so the kernel — which
    # runs as a separate process with its own sys.path — can pick them up
    # without any import magic. Changes to the helpers belong in that module.
    + _CHART_HELPERS_SRC
)


class KernelSession:
    """One kernel subprocess, one run directory, one figure counter."""

    def __init__(self, run_id: UUID, runs_dir: Path) -> None:
        self._run_id = run_id
        self.run_dir = Path(runs_dir) / str(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.figures_dir = self.run_dir / "figures"
        self.figures_dir.mkdir(exist_ok=True)
        self._km: AsyncKernelManager | None = None
        self._client: Any | None = None
        self._figure_index = 0
        self._started = False

    @property
    def run_id(self) -> UUID:
        return self._run_id

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Spawn the kernel subprocess and wait until its channels are ready."""
        if self._started:
            return
        self._km = AsyncKernelManager()
        await self._km.start_kernel(cwd=str(self.run_dir))
        self._client = self._km.client()
        self._client.start_channels()
        await self._client.wait_for_ready(timeout=KERNEL_READY_TIMEOUT_S)
        self._started = True
        # Apply deterministic matplotlib styling (Agg backend, Chinese font
        # fallback chain, high savefig DPI) before any user code runs. This
        # cell is invisible — not recorded in `cells`, no events emitted —
        # so the figure counter and notebook output stay clean.
        await self._execute_silent(_MPL_BOOTSTRAP_SRC)

    async def _execute_silent(self, source: str) -> None:
        """Run a cell and drain iopub, emitting nothing and recording nothing.

        Used for the matplotlib bootstrap. Errors are deliberately swallowed
        — a missing font or a bad rc key must not abort the run.
        """
        assert self._client is not None
        try:
            msg_id = self._client.execute(source, store_history=False)
        except Exception:  # noqa: BLE001
            return
        while True:
            try:
                msg = await asyncio.wait_for(
                    self._client.get_iopub_msg(timeout=KERNEL_READY_TIMEOUT_S),
                    timeout=KERNEL_READY_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001
                return
            parent = msg.get("parent_header") or {}
            if parent.get("msg_id") != msg_id:
                continue
            if (
                msg.get("msg_type") == "status"
                and (msg.get("content") or {}).get("execution_state") == "idle"
            ):
                return

    async def shutdown(self) -> None:
        """Tear down the kernel; swallow errors so `finally` blocks are safe."""
        if not self._started:
            return
        try:
            if self._client is not None:
                self._client.stop_channels()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._km is not None:
                await asyncio.wait_for(self._km.shutdown_kernel(now=True), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self._started = False
        self._client = None
        self._km = None

    # ------------------------------------------------------------------ execute

    async def execute(
        self,
        source: str,
        cell_index: int,
        emitter: EventEmitter,
    ) -> CellExecution:
        """Run one code cell, emit events, and return a CellExecution record."""
        if not self._started:
            await self.start()
        assert self._client is not None and self._km is not None

        t0 = time.monotonic()
        try:
            cell = await asyncio.wait_for(
                self._execute_inner(source, cell_index, emitter),
                timeout=CELL_EXECUTION_TIMEOUT_S,
            )
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self._km.interrupt_kernel()
            cell = CellExecution(
                index=cell_index,
                source=source,
                error=f"execution timed out after {CELL_EXECUTION_TIMEOUT_S}s",
            )
        cell.duration_ms = int((time.monotonic() - t0) * 1000)
        return cell

    async def _execute_inner(
        self,
        source: str,
        cell_index: int,
        emitter: EventEmitter,
    ) -> CellExecution:
        assert self._client is not None

        msg_id = self._client.execute(source, store_history=True)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        result_text: str | None = None
        figure_paths: list[str] = []
        error: str | None = None

        while True:
            try:
                msg = await self._client.get_iopub_msg(timeout=CELL_EXECUTION_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — queue.Empty wrapped in asyncio
                break

            parent = msg.get("parent_header") or {}
            if parent.get("msg_id") != msg_id:
                # Message from a different exec (e.g. leftover) — ignore.
                continue

            msg_type = msg.get("msg_type")
            content = msg.get("content") or {}

            if msg_type == "status":
                # Finished when kernel returns to idle for our msg_id.
                if content.get("execution_state") == "idle":
                    break
                continue

            if msg_type == "stream":
                name = content.get("name", "stdout")
                text = content.get("text", "")
                if not text:
                    continue
                if name == "stderr":
                    stderr_parts.append(text)
                else:
                    stdout_parts.append(text)
                await emitter.emit(
                    "kernel.stdout",
                    {"text": text, "name": name, "cell_index": cell_index},
                    agent="coder",
                )
                continue

            if msg_type in ("execute_result", "display_data"):
                data = content.get("data") or {}
                png_b64 = data.get("image/png")
                if png_b64:
                    fig_path = await self._save_png(png_b64)
                    rel = fig_path.relative_to(self.run_dir).as_posix()
                    figure_paths.append(rel)
                    await emitter.emit(
                        "kernel.figure",
                        {"path": rel, "cell_index": cell_index, "format": "png"},
                        agent="coder",
                    )
                if msg_type == "execute_result":
                    text_plain = data.get("text/plain")
                    if text_plain is not None:
                        result_text = text_plain
                continue

            if msg_type == "error":
                traceback_lines = content.get("traceback") or []
                ename = content.get("ename", "Error")
                evalue = content.get("evalue", "")
                error = f"{ename}: {evalue}"
                # Include the traceback for agent feedback.
                if traceback_lines:
                    error = error + "\n" + "\n".join(traceback_lines)
                # Drain until idle rather than returning immediately, so the
                # next execute() doesn't see stale messages.
                continue

        return CellExecution(
            index=cell_index,
            source=source,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            result_text=result_text,
            figure_paths=figure_paths,
            error=error,
        )

    async def _save_png(self, b64_data: str) -> Path:
        """Write a base64 PNG to `figures/fig-<N>.png`, returning the abs path."""
        idx = self._figure_index
        self._figure_index += 1
        path = self.figures_dir / f"fig-{idx}.png"
        raw = base64.b64decode(b64_data)
        # Blocking write — figures are small (KBs–MBs); async file I/O here would
        # add complexity without measurable benefit.
        path.write_bytes(raw)
        return path

    # ------------------------------------------------------------------ notebook

    async def write_notebook(self, cells: list[CellExecution]) -> Path:
        """Serialise the run's cells to `<run_dir>/notebook.ipynb`."""
        nb = nbformat.v4.new_notebook()
        nb.metadata["kernelspec"] = {
            "name": "python3",
            "display_name": "Python 3",
            "language": "python",
        }
        nb.metadata["language_info"] = {"name": "python"}

        nb_cells: list[Any] = []
        for cell in cells:
            outputs: list[Any] = []
            if cell.stdout:
                outputs.append(
                    nbformat.v4.new_output(
                        output_type="stream", name="stdout", text=cell.stdout
                    )
                )
            if cell.stderr:
                outputs.append(
                    nbformat.v4.new_output(
                        output_type="stream", name="stderr", text=cell.stderr
                    )
                )
            for rel in cell.figure_paths:
                abs_path = self.run_dir / rel
                if abs_path.is_file():
                    encoded = base64.b64encode(abs_path.read_bytes()).decode("ascii")
                    outputs.append(
                        nbformat.v4.new_output(
                            output_type="display_data",
                            data={"image/png": encoded},
                            metadata={},
                        )
                    )
            if cell.result_text is not None:
                outputs.append(
                    nbformat.v4.new_output(
                        output_type="execute_result",
                        data={"text/plain": cell.result_text},
                        metadata={},
                        execution_count=cell.index + 1,
                    )
                )
            if cell.error:
                outputs.append(
                    nbformat.v4.new_output(
                        output_type="error",
                        ename="KernelError",
                        evalue=cell.error.splitlines()[0] if cell.error else "",
                        traceback=cell.error.splitlines() or [""],
                    )
                )

            nb_cells.append(
                nbformat.v4.new_code_cell(
                    source=cell.source,
                    execution_count=cell.index + 1,
                    outputs=outputs,
                )
            )

        nb.cells = nb_cells
        path = self.run_dir / "notebook.ipynb"
        nbformat.write(nb, str(path))
        return path.resolve()


__all__ = ["KernelSession"]
