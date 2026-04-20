"""Jupyter kernel integration for the Coder agent.

Exposes `KernelSession`, a lazy wrapper around `jupyter_client.AsyncKernelManager`
that executes Python code in a subprocess kernel, streams stdout/figures out as
events, and writes the resulting notebook to disk.
"""

from __future__ import annotations

from mm_contracts import CellExecution

from agent_worker.kernel.manager import KernelSession

__all__ = ["CellExecution", "KernelSession"]
