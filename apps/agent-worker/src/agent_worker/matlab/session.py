"""`MatlabSession` — one run's MATLAB/Octave working directory.

Mirrors `KernelSession.execute()` so the CoderAgent can dispatch to either
backend without branching. The session owns a `<runs_dir>/<run_id>/matlab/`
working directory and emits the same event kinds as the Jupyter path
(`kernel.stdout`, `kernel.figure`, `kernel.error`, `log`).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from mm_contracts import CellExecution

from agent_worker.matlab.backends import MatlabBackend, detect_backend

if TYPE_CHECKING:
    from agent_worker.events import EventEmitter


# Match the Jupyter session — 4 KB per stdout chunk so large prints stream
# rather than land in a single jumbo event.
_STDOUT_CHUNK_SIZE = 4096
# Stderr tail size sent in kernel.error.traceback. Anything longer makes the
# event payload unwieldy and the live UI truncates it anyway.
_STDERR_TAIL = 2000


class MatlabSession:
    """One MATLAB/Octave working directory per run.

    Constructing a session is cheap: it makes the working dir and figures
    dir on disk but does not spawn any subprocess. The backend is only
    invoked on `execute()`.
    """

    def __init__(
        self,
        run_id: UUID,
        runs_dir: Path,
        backend: MatlabBackend | None = None,
    ) -> None:
        self._run_id = run_id
        self.run_dir = (Path(runs_dir) / str(run_id)).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # IMPORTANT: backend cwd is the run_dir itself (NOT the matlab/
        # subdir). That way the same `figures/<id>.png` relative path the
        # Coder prompt uses for Python (`plt.savefig('figures/...')`) also
        # works verbatim from MATLAB/Octave. The matlab/ subdir is only a
        # scratch space for the generated _mm_exec_*.m file.
        self._tmp_dir = (self.run_dir / "matlab").resolve()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._cwd = self.run_dir
        self.figures_dir = self.run_dir / "figures"
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        self._backend = backend if backend is not None else detect_backend()

    # ------------------------------------------------------------------ accessors

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def cwd(self) -> Path:
        return self._cwd

    # ------------------------------------------------------------------ execute

    async def execute(
        self,
        source: str,
        cell_index: int,
        emitter: EventEmitter,
        timeout_s: float = 60.0,
    ) -> CellExecution:
        """Run one MATLAB/Octave code block and return a `CellExecution`."""
        await emitter.emit(
            "log",
            {
                "level": "info",
                "message": (
                    f"matlab cell {cell_index} executing via {self._backend.name}"
                ),
            },
            agent="coder",
        )

        before = _snapshot_dir(self.figures_dir)
        t0 = time.monotonic()
        error: str | None = None
        try:
            stdout, stderr, exit_code = await self._backend.run(
                source, self._cwd, timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — surface backend errors as cells
            stdout = ""
            stderr = f"{type(exc).__name__}: {exc}"
            exit_code = 1
            error = stderr
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Stream stdout in chunks so consumers see progress on long prints.
        if stdout:
            for chunk in _chunks(stdout, _STDOUT_CHUNK_SIZE):
                await emitter.emit(
                    "kernel.stdout",
                    {
                        "text": chunk,
                        "name": "stdout",
                        "cell_index": cell_index,
                    },
                    agent="coder",
                )

        # Figures: diff the figures dir snapshot, in deterministic order.
        after = _snapshot_dir(self.figures_dir)
        new_files = sorted(after - before)
        figure_paths: list[str] = []
        for fname in new_files:
            rel = f"figures/{fname}"
            figure_paths.append(rel)
            await emitter.emit(
                "kernel.figure",
                {"path": rel, "cell_index": cell_index, "format": "png"},
                agent="coder",
            )

        # Non-zero exit code → emit a kernel.error event. Use the tail of
        # stderr (rather than the whole thing) to keep the payload small.
        if exit_code != 0:
            if error is None:
                error = (
                    stderr.strip() if stderr.strip() else f"exit code {exit_code}"
                )
            await emitter.emit(
                "kernel.error",
                {
                    "traceback": stderr[-_STDERR_TAIL:],
                    "exit_code": exit_code,
                    "cell_index": cell_index,
                },
                agent="coder",
            )

        return CellExecution(
            index=cell_index,
            source=source,
            stdout=stdout,
            stderr=stderr,
            result_text=None,
            figure_paths=figure_paths,
            error=error,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------- helpers


def _snapshot_dir(path: Path) -> set[str]:
    """Return the set of filenames directly under `path` (no recursion)."""
    try:
        return set(os.listdir(path))
    except FileNotFoundError:
        return set()


def _chunks(text: str, size: int) -> list[str]:
    """Split `text` into `size`-byte slices. Returns at least one chunk."""
    if size <= 0:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


__all__ = ["MatlabSession"]
