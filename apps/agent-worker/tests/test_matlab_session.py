"""`MatlabSession` + backend tests.

Heavy use of `monkeypatch` and `AsyncMock` so the suite runs on any host
regardless of whether MATLAB or Octave is installed. One integration
test exercises a real Octave subprocess and is auto-skipped if the
binary isn't on PATH.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agent_worker.matlab import (
    MatlabBatchBackend,
    MatlabSession,
    NoOpBackend,
    OctaveCliBackend,
    detect_backend,
)


def _stub_emitter() -> Any:
    """Mimic the subset of EventEmitter the session uses."""
    emitter = AsyncMock()
    emitter.emit = AsyncMock(return_value=None)
    emitter.run_id = uuid4()
    return emitter


class _StubBackend:
    """Configurable in-process backend for unit tests."""

    name = "stub"

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        side_effect: Any = None,
        new_figures: list[str] | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.side_effect = side_effect
        self.new_figures = new_figures or []
        self.calls: list[tuple[str, Path, float]] = []

    async def run(
        self, code: str, cwd: Path, timeout_s: float
    ) -> tuple[str, str, int]:
        self.calls.append((code, cwd, timeout_s))
        if self.side_effect is not None:
            raise self.side_effect
        # Write any "produced" figures so the session's diff sees them.
        # MatlabSession sets cwd = run_dir itself so user code paths like
        # `figures/<id>.png` resolve identically to the Python kernel side.
        figures_dir = cwd / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        for fname in self.new_figures:
            (figures_dir / fname).write_bytes(b"\x89PNG\r\n\x1a\n")
        return (self.stdout, self.stderr, self.exit_code)


# ---------------------------------------------------------------- detect_backend


def test_detect_backend_picks_matlab_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(binary: str) -> str | None:
        return "/usr/local/bin/matlab" if binary == "matlab" else None

    monkeypatch.setattr("agent_worker.matlab.backends.shutil.which", fake_which)
    backend = detect_backend()
    assert backend.name == "matlab"
    assert isinstance(backend, MatlabBatchBackend)


def test_detect_backend_falls_back_to_octave(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(binary: str) -> str | None:
        return "/usr/local/bin/octave" if binary == "octave" else None

    monkeypatch.setattr("agent_worker.matlab.backends.shutil.which", fake_which)
    backend = detect_backend()
    assert backend.name == "octave"
    assert isinstance(backend, OctaveCliBackend)


def test_detect_backend_returns_noop_when_nothing_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_worker.matlab.backends.shutil.which", lambda _b: None
    )
    backend = detect_backend()
    assert backend.name == "noop"
    assert isinstance(backend, NoOpBackend)


# ---------------------------------------------------------------- session.execute


async def test_session_execute_captures_stdout_and_exit_code(tmp_path: Path) -> None:
    backend = _StubBackend(stdout="hello matlab\n", stderr="", exit_code=0)
    sess = MatlabSession(uuid4(), tmp_path, backend=backend)

    emitter = _stub_emitter()
    cell = await sess.execute("disp('hello matlab')", cell_index=0, emitter=emitter)

    assert cell.index == 0
    assert cell.source == "disp('hello matlab')"
    assert "hello matlab" in cell.stdout
    assert cell.error is None
    assert cell.duration_ms >= 0
    assert cell.figure_paths == []
    # backend was called with the right cwd
    assert backend.calls and backend.calls[0][1] == sess.cwd
    # log + stdout events were emitted
    kinds = [c.args[0] for c in emitter.emit.call_args_list]
    assert "log" in kinds
    assert "kernel.stdout" in kinds


async def test_session_execute_detects_new_figures_in_figures_dir(
    tmp_path: Path,
) -> None:
    backend = _StubBackend(
        stdout="",
        stderr="",
        exit_code=0,
        new_figures=["plot1.png", "plot2.png"],
    )
    sess = MatlabSession(uuid4(), tmp_path, backend=backend)
    # Pre-existing figure should NOT be reported.
    (sess.figures_dir / "old.png").write_bytes(b"old")

    emitter = _stub_emitter()
    cell = await sess.execute("plot(...)", cell_index=2, emitter=emitter)

    assert sorted(cell.figure_paths) == ["figures/plot1.png", "figures/plot2.png"]
    figure_events = [
        c for c in emitter.emit.call_args_list if c.args[0] == "kernel.figure"
    ]
    assert len(figure_events) == 2
    # Each event carries a relative path under "figures/".
    for ev in figure_events:
        assert ev.args[1]["path"].startswith("figures/")
        assert ev.args[1]["cell_index"] == 2


async def test_session_execute_handles_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hanging subprocess must be killed and reported as an error cell."""

    class _HangingProc:
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            # Sleep longer than any reasonable test timeout.
            await asyncio.sleep(3600)
            return (b"", b"")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or -9

    proc = _HangingProc()

    async def fake_create_subprocess_exec(*_a: Any, **_kw: Any) -> _HangingProc:
        return proc

    # Force the MATLAB binary lookup to succeed.
    monkeypatch.setattr(
        "agent_worker.matlab.backends.shutil.which",
        lambda b: "/usr/bin/matlab" if b == "matlab" else None,
    )
    monkeypatch.setattr(
        "agent_worker.matlab.backends.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    backend = MatlabBatchBackend()
    sess = MatlabSession(uuid4(), tmp_path, backend=backend)
    emitter = _stub_emitter()

    cell = await sess.execute(
        "while true; end", cell_index=0, emitter=emitter, timeout_s=0.05
    )

    assert proc.killed, "expected hanging subprocess to be killed"
    assert cell.error is not None
    assert "timed out" in cell.error.lower()
    kinds = [c.args[0] for c in emitter.emit.call_args_list]
    assert "kernel.error" in kinds


async def test_session_execute_with_noop_backend_returns_error_cell(
    tmp_path: Path,
) -> None:
    sess = MatlabSession(uuid4(), tmp_path, backend=NoOpBackend())
    emitter = _stub_emitter()

    cell = await sess.execute("disp(1)", cell_index=0, emitter=emitter)

    assert cell.error is not None
    assert "MATLAB/Octave not installed" in (cell.error + cell.stderr)
    # The kernel.error event was emitted.
    kinds = [c.args[0] for c in emitter.emit.call_args_list]
    assert "kernel.error" in kinds


# ---------------------------------------------------------------- octave preamble


async def test_octave_backend_prepends_gnuplot_toolkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Octave backend must inject `graphics_toolkit("gnuplot")` for headless plotting."""
    captured: dict[str, Any] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

    def _read_script(path: str) -> str:
        # Read on a thread to satisfy ASYNC240 (no pathlib in async funcs).
        return Path(path).read_text(encoding="utf-8")

    async def fake_create_subprocess_exec(
        *argv: str, cwd: str | None = None, **_kw: Any
    ) -> _FakeProc:
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        # Capture the script's contents at exec time, before the
        # backend cleans it up in its finally block.
        captured["script"] = await asyncio.to_thread(_read_script, argv[-1])
        return _FakeProc()

    monkeypatch.setattr(
        "agent_worker.matlab.backends.shutil.which",
        lambda b: "/usr/bin/octave" if b == "octave" else None,
    )
    monkeypatch.setattr(
        "agent_worker.matlab.backends.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    backend = OctaveCliBackend()
    stdout, stderr, code = await backend.run("disp('hi')", tmp_path, timeout_s=5.0)

    assert code == 0
    assert stdout == ""
    assert stderr == ""
    script = captured["script"]
    # The preamble selects gnuplot but is wrapped in try/catch so a missing
    # toolkit doesn't kill plot-free scripts.
    assert 'graphics_toolkit("gnuplot")' in script.splitlines()[0], script
    assert "disp('hi')" in script
    # argv should be: octave --no-gui --quiet <script.m>
    argv = captured["argv"]
    assert argv[0].endswith("octave")
    assert "--no-gui" in argv
    assert "--quiet" in argv


# ---------------------------------------------------------------- real-octave integration


@pytest.mark.skipif(
    shutil.which("octave") is None, reason="octave not installed on host"
)
async def test_octave_backend_runs_real_script_when_present(tmp_path: Path) -> None:
    backend = OctaveCliBackend()
    sess = MatlabSession(uuid4(), tmp_path, backend=backend)
    emitter = _stub_emitter()

    cell = await sess.execute(
        "disp('mathodology-matlab-smoke')", cell_index=0, emitter=emitter
    )
    assert cell.error is None, cell.stderr
    assert "mathodology-matlab-smoke" in cell.stdout
