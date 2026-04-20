"""`KernelSession` smoke tests — requires a real ipykernel subprocess.

Each test boots a fresh session scoped to `tmp_path` so nothing leaks between
runs. The kernel startup timeout is generous (30s) to handle slow CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import nbformat
import pytest
from agent_worker.kernel import KernelSession


def _stub_emitter() -> Any:
    """Mimic the subset of EventEmitter the kernel uses."""
    emitter = AsyncMock()
    emitter.emit = AsyncMock(return_value=None)
    emitter.run_id = uuid4()
    return emitter


@pytest.fixture
async def session(tmp_path: Path):
    sess = KernelSession(uuid4(), tmp_path)
    try:
        yield sess
    finally:
        await sess.shutdown()


async def test_execute_print_and_expression(session: KernelSession) -> None:
    emitter = _stub_emitter()
    cell = await session.execute(
        "x = 1 + 2\nprint(x)\nx", cell_index=0, emitter=emitter
    )
    assert cell.index == 0
    assert "3" in cell.stdout
    assert cell.result_text is not None and "3" in cell.result_text
    assert cell.error is None
    assert cell.duration_ms >= 0


async def test_execute_error_populates_error_field(session: KernelSession) -> None:
    emitter = _stub_emitter()
    cell = await session.execute(
        "raise ValueError('boom')", cell_index=0, emitter=emitter
    )
    assert cell.error is not None
    assert "ValueError" in cell.error
    assert "boom" in cell.error


async def test_execute_matplotlib_saves_figure(
    session: KernelSession, tmp_path: Path
) -> None:
    emitter = _stub_emitter()
    src = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3])\n"
        "plt.savefig('figures/fig-0.png')\n"
        "plt.close()\n"
    )
    cell = await session.execute(src, cell_index=0, emitter=emitter)
    assert cell.error is None
    fig_path = session.run_dir / "figures" / "fig-0.png"
    assert fig_path.is_file()
    # The agent saved it directly; our global counter only fires on inline
    # image output, so `figure_paths` may be empty here — that's OK for the
    # file-on-disk path which is what the gateway serves.


async def test_execute_inline_figure_emits_event_and_path(
    session: KernelSession,
) -> None:
    emitter = _stub_emitter()
    # Producing a figure as the cell result triggers a `display_data` /
    # `execute_result` message with `image/png`, which KernelSession saves
    # via its internal counter.
    src = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "import io, base64\n"
        "from IPython.display import display, Image\n"
        "fig, ax = plt.subplots()\n"
        "ax.plot([1, 2, 3])\n"
        "buf = io.BytesIO()\n"
        "fig.savefig(buf, format='png')\n"
        "plt.close(fig)\n"
        "display(Image(data=buf.getvalue(), format='png'))\n"
    )
    cell = await session.execute(src, cell_index=0, emitter=emitter)
    assert cell.error is None
    assert cell.figure_paths, "expected inline display to yield a figure_paths entry"
    rel = cell.figure_paths[0]
    assert rel.startswith("figures/"), rel
    assert (session.run_dir / rel).is_file()
    # Verify the emitter received a kernel.figure event.
    kinds = [call.args[0] for call in emitter.emit.call_args_list]
    assert "kernel.figure" in kinds


async def test_write_notebook_produces_valid_ipynb(
    session: KernelSession,
) -> None:
    emitter = _stub_emitter()
    cell = await session.execute("print('hello')", cell_index=0, emitter=emitter)
    path = await session.write_notebook([cell])
    assert path.is_file()
    assert path.name == "notebook.ipynb"

    nb = nbformat.read(str(path), as_version=4)
    assert len(nb.cells) == 1
    assert nb.cells[0].cell_type == "code"
    assert "hello" in nb.cells[0].source or "print" in nb.cells[0].source
