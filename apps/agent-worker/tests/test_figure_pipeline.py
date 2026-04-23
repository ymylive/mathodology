"""Figure contract + pipeline placeholder substitution + paper.meta.json tests.

Covers the M-figures contract:
- `Figure` validation (id slug regex, width range).
- `_substitute_figure_placeholders`: known ids → markdown image; unknown ids
  → silently dropped with a warning.
- `_build_paper_meta`: structure matches what the gateway exporter consumes.
- `CoderAgent` aggregates `directive.figures_saved` into `CoderOutput.figures`
  but only when the PNG actually exists on disk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from agent_worker.agents import CoderAgent
from agent_worker.kernel import KernelSession
from agent_worker.pipeline import (
    _build_paper_meta,
    _substitute_figure_placeholders,
)
from mm_contracts import (
    AnalyzerOutput,
    ApproachSketch,
    Figure,
    ModelSpec,
    PaperDraft,
    PaperSection,
    ProblemInput,
)
from pydantic import ValidationError

# --------------------------------------------------------------------- Figure


def test_figure_accepts_valid_slug_and_defaults() -> None:
    fig = Figure(
        id="sensitivity_alpha",
        caption="敏感性分析",
        path_png="figures/sensitivity_alpha.png",
    )
    assert fig.path_svg is None
    assert fig.width == 0.8


@pytest.mark.parametrize(
    "bad_id",
    ["Sensitivity", "with space", "kebab-case", "", "UPPER"],
)
def test_figure_rejects_non_slug_ids(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Figure(id=bad_id, caption="c", path_png="figures/x.png")


@pytest.mark.parametrize("bad_width", [0.0, -0.1, 1.01, 2.0])
def test_figure_rejects_width_out_of_range(bad_width: float) -> None:
    with pytest.raises(ValidationError):
        Figure(
            id="x",
            caption="c",
            path_png="figures/x.png",
            width=bad_width,
        )


# ---------------------------------------------- figure placeholder substitution


def _paper_with_body(body: str) -> PaperDraft:
    return PaperDraft(
        title="T",
        abstract="A",
        sections=[PaperSection(title="Results", body_markdown=body)],
    )


def test_substitute_known_placeholder_replaces_with_markdown_image() -> None:
    paper = _paper_with_body("Intro.\n[[FIG:rho_vs_l]]\nConclusion.")
    figures = [
        Figure(
            id="rho_vs_l",
            caption="ρ vs L",
            path_png="figures/rho_vs_l.png",
        )
    ]
    out = _substitute_figure_placeholders(paper, figures)
    body = out.sections[0].body_markdown
    assert "[[FIG:" not in body
    assert "![ρ vs L](figures/rho_vs_l.png)" in body
    assert "*图: ρ vs L*" in body


def test_substitute_unknown_placeholder_is_dropped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    paper = _paper_with_body("Before [[FIG:ghost]] after.")
    with caplog.at_level(logging.WARNING):
        out = _substitute_figure_placeholders(paper, figures=[])
    body = out.sections[0].body_markdown
    assert "[[FIG:ghost]]" not in body
    assert body == "Before  after."
    assert any("ghost" in r.message for r in caplog.records)


def test_substitute_multiple_placeholders_across_sections() -> None:
    paper = PaperDraft(
        title="T",
        abstract="A",
        sections=[
            PaperSection(title="Intro", body_markdown="See [[FIG:a]]."),
            PaperSection(title="Results", body_markdown="And [[FIG:b]]."),
        ],
    )
    figs = [
        Figure(id="a", caption="CapA", path_png="figures/a.png"),
        Figure(id="b", caption="CapB", path_png="figures/b.png"),
    ]
    out = _substitute_figure_placeholders(paper, figs)
    assert "CapA" in out.sections[0].body_markdown
    assert "CapB" in out.sections[1].body_markdown


# -------------------------------------------------------- paper.meta.json shape


def test_build_paper_meta_preserves_placeholders_and_problem_fields() -> None:
    problem = ProblemInput(
        problem_text="Solve queue stuff.",
        competition_type="cumcm",
    )
    paper = PaperDraft(
        title="Q Study",
        abstract="Abs",
        sections=[
            PaperSection(
                title="Results",
                body_markdown="See [[FIG:rho]].",
            )
        ],
        references=["Kleinrock 1975"],
    )
    figures = [
        Figure(
            id="rho",
            caption="ρ vs L",
            path_png="figures/rho.png",
            path_svg="figures/rho.svg",
            width=0.7,
        )
    ]

    meta = _build_paper_meta(problem, paper, figures)

    assert meta["title"] == "Q Study"
    assert meta["abstract"] == "Abs"
    assert meta["competition_type"] == "cumcm"
    assert meta["problem_text"] == "Solve queue stuff."
    # Placeholders MUST survive into meta — exporter parses them natively.
    assert "[[FIG:rho]]" in meta["sections"][0]["body_markdown"]
    assert meta["references"] == ["Kleinrock 1975"]
    assert meta["figures"][0]["id"] == "rho"
    assert meta["figures"][0]["path_svg"] == "figures/rho.svg"
    assert meta["figures"][0]["width"] == 0.7

    # And the whole thing round-trips through JSON cleanly.
    assert json.loads(json.dumps(meta, ensure_ascii=False)) == meta


# ---------------------------------- CoderAgent aggregates figures_saved on disk


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
    return ProblemInput(problem_text="Plot something.")


@pytest.fixture
def analysis() -> AnalyzerOutput:
    return AnalyzerOutput(
        restated_problem="Plot something minimal.",
        sub_questions=["Shape?"],
        proposed_approaches=[
            ApproachSketch(name="plot", rationale="trivial", methods=["mpl"])
        ],
    )


@pytest.fixture
def spec() -> ModelSpec:
    return ModelSpec(
        chosen_approach="plot",
        rationale="trivial",
        algorithm_outline=["savefig"],
        validation_strategy="eyeball",
    )


async def test_coder_agent_registers_figures_that_exist_on_disk(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    # The LLM claims two figures; the code only saves one PNG (no SVG). The
    # agent must keep the one on disk and drop the missing one with a warn.
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3])\n"
        "plt.savefig('figures/trend.png')\n"
        "plt.close()\n"
    )
    directive = {
        "reasoning": "save one figure",
        "code": code,
        "done": True,
        "summary": "done",
        "figures_saved": [
            {"id": "trend", "caption": "Trend line", "width": 0.8},
            {"id": "missing", "caption": "Not saved", "width": 0.8},
        ],
    }
    gateway = _FakeGateway([json.dumps(directive)])
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    output = await agent.run(problem, analysis, spec)

    assert len(output.figures) == 1
    fig = output.figures[0]
    assert fig.id == "trend"
    assert fig.caption == "Trend line"
    assert fig.path_png == "figures/trend.png"
    assert fig.path_svg is None  # SVG was not saved
    # Back-compat path list still populated.
    assert "figures/trend.png" in output.figure_paths
    # A warning log was emitted for the missing figure.
    logs = [e for e in emitter.events if e[0] == "log"]
    assert any("missing" in (e[1].get("message") or "") for e in logs)


async def test_coder_agent_picks_up_sibling_svg_when_present(
    tmp_path: Path,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    spec: ModelSpec,
) -> None:
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3])\n"
        "plt.savefig('figures/trend.png')\n"
        "plt.savefig('figures/trend.svg')\n"
        "plt.close()\n"
    )
    directive = {
        "reasoning": "save both formats",
        "code": code,
        "done": True,
        "summary": "done",
        "figures_saved": [
            {"id": "trend", "caption": "T", "width": 0.9},
        ],
    }
    gateway = _FakeGateway([json.dumps(directive)])
    emitter = _FakeEmitter()
    kernel = KernelSession(uuid4(), tmp_path)

    agent = CoderAgent(gateway, emitter, kernel)  # type: ignore[arg-type]
    output = await agent.run(problem, analysis, spec)

    assert len(output.figures) == 1
    assert output.figures[0].path_svg == "figures/trend.svg"
    assert output.figures[0].width == 0.9
