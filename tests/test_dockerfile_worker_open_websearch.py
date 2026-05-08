"""Regression guard: Dockerfile.worker must include open-webSearch MCP runtime.

Without Node + the npm package globally installed in the worker image,
Baidu/CSDN/Juejin/etc go silent (the Python config falls back to
`npx open-websearch` which fails with command-not-found). Verified during
v0.5.2 end-to-end testing — log line "primary=open_websearch: 0 papers
across 5 engines" with reasoning_effort=low on a Chinese problem.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_worker_includes_node_and_websearch_mcp() -> None:
    contents = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    code_lines = [
        line for line in contents.splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)

    # Node runtime (any reasonable installation marker).
    assert "nodesource" in code.lower() or "nodejs" in code.lower(), (
        "Dockerfile.worker must install Node.js for the open-webSearch MCP"
    )
    # The MCP package install line.
    assert "open-websearch" in code, (
        "Dockerfile.worker must `npm install -g open-websearch` so the "
        "Searcher can launch the MCP via open_websearch_cmd"
    )
    # The env var pointing at the launch command.
    assert "OPEN_WEBSEARCH_CMD" in code, (
        "Dockerfile.worker must set OPEN_WEBSEARCH_CMD so the worker "
        "process knows how to spawn the MCP"
    )
