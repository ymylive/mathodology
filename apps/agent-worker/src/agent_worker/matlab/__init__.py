"""MATLAB/Octave execution layer for the Coder agent.

Mirrors the Jupyter `KernelSession` API so the Coder can run MATLAB-style
code blocks alongside Python ones. Backend selection is automatic:
real MATLAB (`matlab -batch`) is preferred, Octave (`octave --no-gui`)
is the fallback, and a NoOp backend reports a graceful error when
neither is installed.
"""

from __future__ import annotations

from agent_worker.matlab.backends import (
    BackendUnavailable,
    MatlabBackend,
    MatlabBatchBackend,
    NoOpBackend,
    OctaveCliBackend,
    detect_backend,
)
from agent_worker.matlab.session import MatlabSession

__all__ = [
    "BackendUnavailable",
    "MatlabBackend",
    "MatlabBatchBackend",
    "MatlabSession",
    "NoOpBackend",
    "OctaveCliBackend",
    "detect_backend",
]
