"""Subprocess backends for MATLAB / Octave execution.

Each backend exposes a single coroutine `run(code, cwd, timeout_s)` that
returns `(stdout, stderr, exit_code)`. Backends never raise on user code
errors — they propagate the exit code so the caller can build a
`CellExecution` record. They DO raise `BackendUnavailable` if the
underlying binary disappears between detection and run-time.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4


class BackendUnavailable(RuntimeError):
    """Raised when the chosen backend's binary is not on PATH at run-time."""


@runtime_checkable
class MatlabBackend(Protocol):
    """Protocol every concrete backend must satisfy."""

    name: str

    async def run(
        self,
        code: str,
        cwd: Path,
        timeout_s: float,
    ) -> tuple[str, str, int]:
        """Execute `code` in `cwd` and return (stdout, stderr, exit_code).

        Implementations must kill the subprocess on `timeout_s` and surface
        a non-zero exit code with a descriptive stderr message rather than
        bubbling `TimeoutError`.
        """
        ...


async def _run_subprocess(
    argv: list[str],
    cwd: Path,
    timeout_s: float,
) -> tuple[str, str, int]:
    """Spawn `argv`, capture stdout/stderr, kill on timeout.

    Returns (stdout, stderr, exit_code). A timeout yields exit_code=124
    (the conventional shell value for `timeout(1)`) and an explanatory
    stderr suffix.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return (
            "",
            f"execution timed out after {timeout_s:.1f}s",
            124,
        )
    return (
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
        proc.returncode if proc.returncode is not None else -1,
    )


class MatlabBatchBackend:
    """Runs code via `matlab -batch`. Each call writes a temp `.m` file."""

    name = "matlab"

    async def run(
        self,
        code: str,
        cwd: Path,
        timeout_s: float,
    ) -> tuple[str, str, int]:
        binary = shutil.which("matlab")
        if binary is None:
            raise BackendUnavailable("matlab binary not found on PATH")
        # MATLAB -batch wants a function/script name, not a path. We add the
        # parent dir to the MATLAB path and call by stem to keep the command
        # portable when cwd contains spaces.
        script = cwd / f"_mm_exec_{uuid4().hex}.m"
        script.write_text(code, encoding="utf-8")
        try:
            stem = script.stem
            argv = [
                binary,
                "-batch",
                f"addpath('{cwd}'); {stem}",
            ]
            return await _run_subprocess(argv, cwd, timeout_s)
        finally:
            with contextlib.suppress(OSError):
                script.unlink()


class OctaveCliBackend:
    """Runs code via `octave --no-gui --quiet <script.m>`.

    The `.m` script is preferred over `--eval` because it survives quotes
    and newlines in user code. `graphics_toolkit("gnuplot")` is prepended
    so headless plot calls don't crash on macOS / Linux without a display.
    """

    name = "octave"

    # Wrapped in try/catch so a missing gnuplot install doesn't abort
    # plot-free scripts. When gnuplot IS available this still selects it,
    # which is what we want for headless rendering on macOS / Linux.
    _PREAMBLE = (
        'try; graphics_toolkit("gnuplot"); catch; end_try_catch;\n'
    )

    async def run(
        self,
        code: str,
        cwd: Path,
        timeout_s: float,
    ) -> tuple[str, str, int]:
        binary = shutil.which("octave")
        if binary is None:
            raise BackendUnavailable("octave binary not found on PATH")
        script = cwd / f"_mm_exec_{uuid4().hex}.m"
        script.write_text(self._PREAMBLE + code, encoding="utf-8")
        try:
            argv = [
                binary,
                "--no-gui",
                "--quiet",
                str(script),
            ]
            return await _run_subprocess(argv, cwd, timeout_s)
        finally:
            with contextlib.suppress(OSError):
                script.unlink()


class NoOpBackend:
    """Sentinel backend used when no MATLAB-compatible runtime is present.

    Returns a synthetic failure rather than raising so the session can
    build a `CellExecution` with a friendly error message.
    """

    name = "noop"

    async def run(
        self,
        code: str,
        cwd: Path,
        timeout_s: float,
    ) -> tuple[str, str, int]:
        # Trivial await to keep this honestly async — and to make sure the
        # event loop gets a chance to schedule other tasks.
        await asyncio.sleep(0)
        del code, cwd, timeout_s
        return ("", "MATLAB/Octave not installed", 127)


def detect_backend() -> MatlabBackend:
    """Pick the best available backend.

    Order: MATLAB (proprietary, better fidelity) → Octave → NoOp.
    """
    if shutil.which("matlab") is not None:
        return MatlabBatchBackend()
    if shutil.which("octave") is not None:
        return OctaveCliBackend()
    return NoOpBackend()


__all__ = [
    "BackendUnavailable",
    "MatlabBackend",
    "MatlabBatchBackend",
    "NoOpBackend",
    "OctaveCliBackend",
    "detect_backend",
]
