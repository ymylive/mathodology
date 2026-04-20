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


class Attachment(BaseModel):
    """One uploaded file attached to a problem submission."""

    name: str
    kind: AttachmentKind
    content_base64: str | None = None


class ProblemInput(BaseModel):
    """Problem payload submitted by the user, carried on the `mm:jobs` stream."""

    problem_text: str
    competition_type: CompetitionType = "other"
    attachments: list[Attachment] = Field(default_factory=list)
    model_override: str | None = None


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
    sub_questions: list[str] = Field(min_length=1, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=15)
    data_requirements: list[DataRequirement] = Field(default_factory=list, max_length=10)
    proposed_approaches: list[ApproachSketch] = Field(min_length=1, max_length=3)


class ModelSpec(BaseModel):
    """Modeler agent output. Stub for M1."""

    name: str = ""


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


class PaperDraft(BaseModel):
    """Writer agent output. Stub for M1."""

    markdown: str = ""


class RunResult(BaseModel):
    """Terminal result for one run. Stub for M1."""

    status: Literal["success", "failed", "cancelled"] = "success"
