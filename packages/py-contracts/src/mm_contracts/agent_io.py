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


class AnalyzerOutput(BaseModel):
    """Analyzer agent output. Stub for M1."""

    summary: str = ""


class ModelSpec(BaseModel):
    """Modeler agent output. Stub for M1."""

    name: str = ""


class CoderOutput(BaseModel):
    """Coder agent output. Stub for M1."""

    notebook_path: str = ""


class PaperDraft(BaseModel):
    """Writer agent output. Stub for M1."""

    markdown: str = ""


class RunResult(BaseModel):
    """Terminal result for one run. Stub for M1."""

    status: Literal["success", "failed", "cancelled"] = "success"
