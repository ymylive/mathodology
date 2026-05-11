"""WriterAgent smoke test + `_render_paper_markdown` unit test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from agent_worker.agents import WriterAgent
from agent_worker.pipeline import _render_paper_markdown
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    CellExecution,
    CoderOutput,
    ModelSpec,
    PaperDraft,
    PaperSection,
    ProblemInput,
    SearchFindings,
)


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(
        self, kind: str, payload: dict | None = None, agent: str | None = None
    ) -> None:
        self.events.append((kind, payload or {}, agent))


class _FakeGateway:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        for c in self._chunks:
            yield c

    async def close(self) -> None:
        pass


@pytest.fixture
def problem() -> ProblemInput:
    return ProblemInput(
        problem_text="Model a simple queue and compute steady-state length.",
        competition_type="mcm",
    )


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Model a single-server queue and report its mean length.",
        sub_questions=["What is the stationary distribution?"],
        proposed_approaches=[
            ApproachSketch(name="M/M/1", rationale="canonical", methods=["analytic"]),
        ],
    )


@pytest.fixture
def spec() -> ModelSpec:
    return ModelSpec(
        chosen_approach="M/M/1",
        rationale="Poisson arrivals, exponential service.",
        algorithm_outline=["Compute rho", "Compute L"],
        validation_strategy="sim vs analytic",
    )


@pytest.fixture
def coder_output() -> CoderOutput:
    return CoderOutput(
        cells=[
            CellExecution(
                index=0,
                source="print(1+2)",
                stdout="3\n",
                result_text=None,
            )
        ],
        figure_paths=["figures/fig-0.png"],
        final_summary="L ~ 4.0 at rho=0.8",
        notebook_path="/tmp/runs/abc/notebook.ipynb",
    )


async def test_writer_agent_returns_validated_paper(
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_output: CoderOutput,
) -> None:
    paper_json = (
        '{"title":"A Queueing Study",'
        '"abstract":"We model a single-server queue.",'
        '"sections":['
        '{"title":"Introduction","body_markdown":"We study $L$ for an M/M/1 queue."},'
        '{"title":"Results","body_markdown":"![L vs rho](figures/fig-0.png)"}'
        "],"
        '"references":["Kleinrock 1975"],'
        '"figure_refs":["figures/fig-0.png"]}'
    )
    gateway = _FakeGateway([paper_json])
    emitter = _FakeEmitter()

    agent = WriterAgent(gateway, emitter)  # type: ignore[arg-type]
    paper = await agent.run_for(problem, analysis, spec, coder_output)

    assert isinstance(paper, PaperDraft)
    assert paper.title
    assert paper.abstract
    assert len(paper.sections) == 2
    assert paper.figure_refs == ["figures/fig-0.png"]

    kinds = [e[0] for e in emitter.events]
    assert "stage.start" in kinds
    assert "agent.output" in kinds
    assert "stage.done" in kinds


async def test_writer_includes_paper_fulltexts_when_paths_present(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_output: CoderOutput,
) -> None:
    run_dir = tmp_path / "run"
    papers_dir = run_dir / "papers"
    papers_dir.mkdir(parents=True)
    (papers_dir / "01.md").write_text("paper 1 full text", encoding="utf-8")
    (papers_dir / "02.md").write_text("paper 2 full text", encoding="utf-8")

    captured_messages: list[dict] = []

    class _CapturingGateway:
        async def stream_completion(self, **kwargs: object) -> AsyncIterator[str]:
            msgs = kwargs.get("messages") or []
            assert isinstance(msgs, list)
            captured_messages.extend(msgs)  # type: ignore[arg-type]
            yield (
                '{"title":"T","abstract":"a",'
                '"sections":[{"title":"Intro","body_markdown":"x"}],'
                '"references":[],"figure_refs":[]}'
            )

        async def close(self) -> None:
            pass

    emitter = _FakeEmitter()
    findings = SearchFindings(
        queries=["q"],
        papers=[],
        key_findings=[],
        datasets_mentioned=[],
        paper_fulltext_paths=["papers/01.md", "papers/02.md"],
    )

    agent = WriterAgent(_CapturingGateway(), emitter, run_dir=run_dir)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis, spec, coder_output, findings)

    body = "\n".join(str(m.get("content", "")) for m in captured_messages)
    assert "paper 1 full text" in body
    assert "paper 2 full text" in body
    # Numbered cite hint surfaces in the user prompt.
    assert "Paper 1" in body
    assert "Paper 2" in body


async def test_writer_omits_fulltexts_when_no_paths(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_output: CoderOutput,
) -> None:
    """Empty `paper_fulltext_paths` → no Paper N block + no errors."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    paper_json = (
        '{"title":"T","abstract":"a",'
        '"sections":[{"title":"Intro","body_markdown":"x"}],'
        '"references":[],"figure_refs":[]}'
    )
    gateway = _FakeGateway([paper_json])
    emitter = _FakeEmitter()
    findings = SearchFindings(queries=[], papers=[], key_findings=[])

    agent = WriterAgent(gateway, emitter, run_dir=run_dir)  # type: ignore[arg-type]
    paper = await agent.run_for(problem, analysis, spec, coder_output, findings)
    assert isinstance(paper, PaperDraft)


async def test_writer_truncates_oversized_fulltext(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
    coder_output: CoderOutput,
) -> None:
    """Oversized paper file is truncated at paragraph boundary."""
    from agent_worker.agents.writer import WRITER_SOFT_TRUNCATE_CHARS

    run_dir = tmp_path / "run"
    papers_dir = run_dir / "papers"
    papers_dir.mkdir(parents=True)
    # Build a file well over the soft cap, with regular paragraph
    # boundaries so the rsplit("\n\n", 1) lands on one.
    paragraph = ("x" * 100 + "\n\n") * (WRITER_SOFT_TRUNCATE_CHARS // 102 + 50)
    (papers_dir / "01.md").write_text(paragraph, encoding="utf-8")

    captured_messages: list[dict] = []

    class _CapturingGateway:
        async def stream_completion(self, **kwargs: object) -> AsyncIterator[str]:
            msgs = kwargs.get("messages") or []
            assert isinstance(msgs, list)
            captured_messages.extend(msgs)  # type: ignore[arg-type]
            yield (
                '{"title":"T","abstract":"a",'
                '"sections":[{"title":"Intro","body_markdown":"x"}],'
                '"references":[],"figure_refs":[]}'
            )

        async def close(self) -> None:
            pass

    emitter = _FakeEmitter()
    findings = SearchFindings(
        queries=["q"],
        papers=[],
        key_findings=[],
        paper_fulltext_paths=["papers/01.md"],
    )

    agent = WriterAgent(_CapturingGateway(), emitter, run_dir=run_dir)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis, spec, coder_output, findings)

    body = "\n".join(str(m.get("content", "")) for m in captured_messages)
    assert "[...truncated]" in body


def test_render_paper_markdown_has_title_and_abstract() -> None:
    paper = PaperDraft(
        title="Queueing Study",
        abstract="Abstract body.",
        sections=[
            PaperSection(title="Introduction", body_markdown="Intro paragraph."),
            PaperSection(title="Results", body_markdown="Results paragraph."),
        ],
        references=["Kleinrock 1975", "Little 1961"],
    )
    md = _render_paper_markdown(paper)
    assert "# Queueing Study" in md
    assert "## Abstract" in md
    assert "Abstract body." in md
    assert "## Introduction" in md
    assert "## Results" in md
    assert "## References" in md
    assert "1. Kleinrock 1975" in md
    assert "2. Little 1961" in md


def test_render_paper_markdown_without_references() -> None:
    paper = PaperDraft(
        title="Short Note",
        abstract="Tiny.",
        sections=[PaperSection(title="Only", body_markdown="Body.")],
    )
    md = _render_paper_markdown(paper)
    assert "# Short Note" in md
    assert "## Abstract" in md
    assert "## References" not in md
