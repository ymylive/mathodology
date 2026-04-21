"""Hand-written Pydantic v2 contracts for agent I/O.

These models mirror `packages/contracts/events.schema.json` and the worker-side
shapes used by the 4-agent pipeline. Only the bits needed for M1 are fleshed
out; downstream agent outputs are stubs that will be filled in later milestones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Input / ingress
# ---------------------------------------------------------------------------


AttachmentKind = Literal["csv", "xlsx", "pdf", "image", "text"]
CompetitionType = Literal["mcm", "icm", "cumcm", "huashu", "other"]
ReasoningEffort = Literal["off", "low", "medium", "high"]


class Attachment(BaseModel):
    """One uploaded file attached to a problem submission."""

    name: str
    kind: AttachmentKind
    content_base64: str | None = None


class ProblemInput(BaseModel):
    """Problem payload submitted by the user, carried on the `mm:jobs` stream."""

    model_config = ConfigDict(extra="forbid")

    problem_text: str
    competition_type: CompetitionType = "other"
    attachments: list[Attachment] = Field(default_factory=list)
    model_override: str | None = None
    reasoning_effort: ReasoningEffort = "medium"
    # Opt-in to a 1M-token max_tokens ceiling for models that advertise
    # long-context. When false, we cap at 20k (safe for all providers).
    # User-supplied: disable for default models, enable only for 1M-
    # capable variants (Claude 3.5 Sonnet 1M beta, Gemini 2.0, gpt-5-1m).
    long_context: bool = False


# ---------------------------------------------------------------------------
# Event envelope (mirrors events.schema.json)
# ---------------------------------------------------------------------------


AgentName = Literal[
    "analyzer",
    "modeler",
    "coder",
    "writer",
    "critic",
    "searcher",
]

EventKind = Literal[
    "stage.start",
    "stage.done",
    "log",
    "token",
    "cost",
    "agent.output",
    "kernel.stdout",
    "kernel.figure",
    "error",
    "done",
]


class AgentEvent(BaseModel):
    """Envelope for every event on `mm:events:<run_id>`.

    `seq` is monotonic per run. `payload` is kind-specific; see
    `events.schema.json#/$defs` for the per-kind shapes.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    agent: AgentName | None = None
    kind: EventKind
    seq: int = Field(ge=0)
    ts: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cost / token accounting
# ---------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """Token + cost rollup for a single LLM call."""

    prompt: int
    completion: int
    total: int
    model: str
    cost_rmb: float


# ---------------------------------------------------------------------------
# Agent output stubs (M1 placeholders — filled in M3+)
# ---------------------------------------------------------------------------


class DataRequirement(BaseModel):
    """One piece of data the solver will need, with an optional source hint."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    source_hint: str | None = None  # "kaggle", "dataset.gov", or a URL


class ApproachSketch(BaseModel):
    """A candidate modeling approach the Modeler can choose from."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rationale: str
    methods: list[str] = Field(default_factory=list, max_length=10)


class AnalyzerOutput(BaseModel):
    """Analyzer agent output: scope + sub-questions + approach sketches."""

    model_config = ConfigDict(extra="forbid")

    restated_problem: str = Field(min_length=10)
    # Relaxed caps — real MCM problems at high reasoning routinely exceed
    # the earlier tight limits (observed: Modeler returned 38 variables /
    # 24 equations for a stochastic EV planning problem, forcing a retry
    # loop + eventual parse failure). Keep soft caps to bound memory use.
    sub_questions: list[str] = Field(min_length=1, max_length=30)
    assumptions: list[str] = Field(default_factory=list, max_length=40)
    data_requirements: list[DataRequirement] = Field(default_factory=list, max_length=25)
    proposed_approaches: list[ApproachSketch] = Field(min_length=1, max_length=6)


class Variable(BaseModel):
    """One variable in the Modeler's spec, with LaTeX symbol and units."""

    model_config = ConfigDict(extra="forbid")

    symbol: str  # e.g. "\\lambda_i"
    name: str  # human-readable
    unit: str | None = None
    description: str


class Equation(BaseModel):
    """One equation in the Modeler's spec, written in LaTeX."""

    model_config = ConfigDict(extra="forbid")

    latex: str  # LaTeX without surrounding $$; e.g. "\\lambda_i = f(t)"
    description: str  # short prose


class MethodNode(BaseModel):
    """A single entry in the HMML (Hierarchical Math Modeling Library) knowledge base."""

    model_config = ConfigDict(extra="forbid")

    id: str  # slug, e.g. "ols_linear_regression"
    name: str  # display name
    domain: str  # top-level: optimization, statistics, ...
    subdomain: str  # e.g. "linear_models", "combinatorial"
    applicable_scenarios: list[str] = Field(min_length=1, max_length=10)
    math_form: str  # short LaTeX summary of the canonical equation(s)
    python_template: str  # minimal working snippet (stdlib + numpy/scipy)
    typical_cases: list[str] = Field(default_factory=list, max_length=10)
    common_pitfalls: list[str] = Field(default_factory=list, max_length=10)
    keywords: list[str] = Field(default_factory=list, max_length=15)


class ConsultedMethod(BaseModel):
    """How Modeler reported which HMML methods it reviewed."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    reason: str  # short phrase, e.g. "selected as primary",
    # "considered but inferior because ...",
    # "partial basis for hybrid approach"


class ModelSpec(BaseModel):
    """Modeler agent output: one chosen modeling approach, fully specified."""

    model_config = ConfigDict(extra="forbid")

    chosen_approach: str  # one-line approach name, e.g. "M/M/c per station"
    rationale: str  # why this approach fits the problem
    variables: list[Variable] = Field(default_factory=list, max_length=80)
    equations: list[Equation] = Field(default_factory=list, max_length=50)
    algorithm_outline: list[str] = Field(min_length=1, max_length=40)
    complexity_notes: str | None = None
    validation_strategy: str
    consulted_methods: list[ConsultedMethod] = Field(
        default_factory=list, max_length=10
    )


class CellExecution(BaseModel):
    """One executed notebook cell captured from the Jupyter kernel."""

    model_config = ConfigDict(extra="forbid")

    index: int
    source: str
    stdout: str = ""
    stderr: str = ""
    result_text: str | None = None  # text/plain repr of the last expression
    figure_paths: list[str] = Field(
        default_factory=list
    )  # relative to run dir, e.g. figures/fig-0.png
    error: str | None = None  # kernel error message, if any
    duration_ms: int = 0


class CoderOutput(BaseModel):
    """Coder agent output: executed cells, saved figures, final summary."""

    model_config = ConfigDict(extra="forbid")

    cells: list[CellExecution]
    figure_paths: list[str] = Field(default_factory=list)
    final_summary: str  # plain-text final answer from the agent
    notebook_path: str  # absolute path to the written .ipynb


class PaperSection(BaseModel):
    """One section of the paper — title + Markdown body (with LaTeX inline)."""

    model_config = ConfigDict(extra="forbid")

    title: str
    body_markdown: str  # Markdown with $LaTeX$ and $$display$$ inline


class PaperDraft(BaseModel):
    """Writer agent output: publication-grade paper as structured sections."""

    model_config = ConfigDict(extra="forbid")

    title: str
    abstract: str
    sections: list[PaperSection] = Field(min_length=1, max_length=12)
    references: list[str] = Field(default_factory=list, max_length=30)
    figure_refs: list[str] = Field(
        default_factory=list
    )  # relative paths under runs/<id>/


# ---------------------------------------------------------------------------
# Searcher agent (M10)
# ---------------------------------------------------------------------------


class Paper(BaseModel):
    """One retrieved paper (arXiv in M10; extensible to other sources later)."""

    model_config = ConfigDict(extra="forbid")

    title: str
    authors: list[str] = Field(default_factory=list, max_length=20)
    abstract: str = ""  # may be empty if unavailable
    url: str  # canonical arXiv abs URL
    arxiv_id: str | None = None  # e.g. "2312.01234"
    published: str | None = None  # ISO 8601 date
    relevance_reason: str | None = None  # LLM-assigned, why it matters here


class SearchFindings(BaseModel):
    """Searcher agent output: curated papers + synthesized key findings."""

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(default_factory=list, max_length=10)
    papers: list[Paper] = Field(default_factory=list, max_length=15)
    key_findings: list[str] = Field(default_factory=list, max_length=10)
    # short synthesized bullets for Writer
    datasets_mentioned: list[str] = Field(default_factory=list, max_length=10)


class RunResult(BaseModel):
    """Terminal result for one run. Stub for M1."""

    status: Literal["success", "failed", "cancelled"] = "success"
