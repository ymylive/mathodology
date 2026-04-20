"""CLI entrypoint: `python -m agent_worker` and `agent-worker` script."""

from __future__ import annotations

import asyncio

from agent_worker import main


def run() -> None:
    """Synchronous entrypoint used by `[project.scripts]` and `__main__`."""
    asyncio.run(main.run())


if __name__ == "__main__":
    run()
