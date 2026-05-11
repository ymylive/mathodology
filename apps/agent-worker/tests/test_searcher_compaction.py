"""Tests for SearcherAgent._compact_oversized_papers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from agent_worker.agents import SearcherAgent
from agent_worker.agents.searcher import COMPACT_THRESHOLD_CHARS


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(self, kind, payload=None, agent=None):  # noqa: ANN001
        self.events.append((kind, payload or {}, agent))


class _ScriptedGateway:
    def __init__(self, response: str | Exception) -> None:
        self._response = response

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        if isinstance(self._response, Exception):
            raise self._response
        yield self._response

    async def close(self) -> None:
        pass


def _make_agent(response):  # noqa: ANN001
    return SearcherAgent(_ScriptedGateway(response), _FakeEmitter())  # type: ignore[arg-type]


async def test_compact_skips_files_under_threshold(tmp_path: Path) -> None:
    """Files under the threshold are not touched and the LLM is not called."""
    agent = _make_agent(RuntimeError("LLM must not be called for short files"))
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    short = "short content" * 100  # well below 24k chars
    (runs_papers_dir / "01.md").write_text(short, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert paths == ["papers/01.md"]
    assert (runs_papers_dir / "01.md").read_text("utf-8") == short


async def test_compact_overwrites_oversized_file_with_llm_output(tmp_path: Path) -> None:
    # Must exceed COMPACT_MIN_OUTPUT_CHARS (1000) to be accepted as usable.
    compacted = "## Methods\n\n" + ("dense " * 250)
    agent = _make_agent(compacted)
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    long_text = "x" * (COMPACT_THRESHOLD_CHARS + 1000)
    (runs_papers_dir / "01.md").write_text(long_text, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert paths == ["papers/01.md"]
    # _compact_one strips the LLM output before persisting.
    assert (runs_papers_dir / "01.md").read_text("utf-8") == compacted.strip()


async def test_compact_keeps_raw_on_llm_failure(tmp_path: Path) -> None:
    agent = _make_agent(RuntimeError("provider 502"))
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    long_text = "x" * (COMPACT_THRESHOLD_CHARS + 1000)
    (runs_papers_dir / "01.md").write_text(long_text, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert paths == ["papers/01.md"]
    assert (runs_papers_dir / "01.md").read_text("utf-8") == long_text


async def test_compact_keeps_raw_when_llm_output_too_short(tmp_path: Path) -> None:
    agent = _make_agent("tiny")  # << COMPACT_MIN_OUTPUT_CHARS
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    long_text = "x" * (COMPACT_THRESHOLD_CHARS + 1000)
    (runs_papers_dir / "01.md").write_text(long_text, encoding="utf-8")

    await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert (runs_papers_dir / "01.md").read_text("utf-8") == long_text
