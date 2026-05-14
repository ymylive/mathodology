"""Post-stage hook contract — borrowed from claude-code-sourcemap.

A *post-stage hook* runs after an agent produces structured output. It can
either approve (default) or block the run, AND it can inject an
`additional_context` string that downstream stages see as a
`<system_reminder>` block in their user prompt. That mechanism is how
claude-code threads non-fatal critique feedback forward without polluting
the system prompt (which would bust the prompt cache).

For our pipeline we treat the existing Critic as the canonical hook and
provide an adapter (`critique_to_hook_result`). Future hooks (judge
simulation, terminology consistency check, page-count gate) can plug into
the same protocol without touching `pipeline.py`.

Sourcemap reference: `restored-src/src/types/hooks.ts:50-166`
(`syncHookResponseSchema` discriminated union) and
`restored-src/src/utils/hooks.ts:550-660` (the dispatch + aggregation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from mm_contracts import CritiqueReport


class PostStageOutput(BaseModel):
    """Per-event payload returned by a `PostStage` hook."""

    model_config = ConfigDict(extra="forbid")

    hook_event_name: Literal["PostStage"] = "PostStage"
    # Free-text reminder; downstream stages render this verbatim as a
    # `<system_reminder>` block inside their user prompt.
    additional_context: str | None = None
    # Optional patched artifact — when the hook wants to mutate the output
    # in-place (e.g. dedupe references, strip leaked school name). Currently
    # advisory; the pipeline doesn't actually apply it yet.
    updated_artifact: dict[str, Any] | None = None


class HookResult(BaseModel):
    """Normalized return for any post-stage hook.

    Mirrors sourcemap's `syncHookResponseSchema`. The pipeline aggregates
    these via `AggregatedHookResult` after running all hooks for one stage.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "block"] | None = None
    reason: str | None = Field(
        None, description="Short LLM-visible explanation of the decision."
    )
    system_message: str | None = Field(
        None,
        description="User-visible (non-LLM) warning, e.g. for log/audit.",
    )
    hook_specific_output: PostStageOutput | None = None
    # Sourcemap exposes this for transport — we keep it so the contract
    # stays compatible with the JS protocol, even though we ignore it
    # internally (`break` is conveyed via `decision="block"`).
    stop_reason: str | None = None


class AggregatedHookResult(BaseModel):
    """Combined verdict across all hooks for one (stage, output) pair."""

    model_config = ConfigDict(extra="forbid")

    blocking_errors: list[str] = Field(default_factory=list)
    additional_contexts: list[str] = Field(default_factory=list)
    updated_artifact: dict[str, Any] | None = None

    @property
    def should_block(self) -> bool:
        return bool(self.blocking_errors)

    def merged_reminder(self) -> str:
        """Render `additional_contexts` as a single `<system_reminder>` block.

        Returns the empty string when there are no reminders so callers can
        unconditionally interpolate it into prompt templates.
        """
        cleaned = [c.strip() for c in self.additional_contexts if c and c.strip()]
        if not cleaned:
            return ""
        joined = "\n\n".join(cleaned)
        return f"<system_reminder>\n{joined}\n</system_reminder>"


def aggregate(results: list[HookResult]) -> AggregatedHookResult:
    """Merge a list of `HookResult`s into a single verdict."""
    agg = AggregatedHookResult()
    for r in results:
        if r.decision == "block" and r.reason:
            agg.blocking_errors.append(r.reason)
        if r.hook_specific_output is not None:
            extra = r.hook_specific_output.additional_context
            if extra and extra.strip():
                agg.additional_contexts.append(extra.strip())
            if r.hook_specific_output.updated_artifact is not None:
                # Last-writer wins on conflicting patches; in practice
                # only one hook should propose an updated_artifact per stage.
                agg.updated_artifact = r.hook_specific_output.updated_artifact
    return agg


def critique_to_hook_result(report: CritiqueReport) -> HookResult:
    """Adapt the existing `CritiqueReport` to the post-stage hook protocol.

    The adapter encodes Critic verdicts as:
    - `decision="block"` ⇔ has blocking findings or `passed is False`
      with major findings
    - `additional_context` is a short bullet list of remaining minor
      findings — these flow into the next stage's user prompt as a
      `<system_reminder>` reminding the model not to propagate them.
    """
    has_blocking = any(
        getattr(f, "severity", "") == "blocking" for f in report.findings
    )
    major_count = sum(
        1 for f in report.findings if getattr(f, "severity", "") == "major"
    )
    minor_findings = [
        f for f in report.findings if getattr(f, "severity", "") in {"minor", "info"}
    ]
    decision: Literal["approve", "block"]
    decision = "block" if (has_blocking or major_count >= 2) else "approve"

    # Build a concise downstream reminder from minor + the report summary.
    lines: list[str] = []
    if report.summary:
        lines.append(f"Upstream Critic summary ({report.target_agent}): {report.summary}")
    for f in minor_findings[:5]:
        msg = getattr(f, "message", "")
        area = getattr(f, "area", "")
        if msg:
            prefix = f"[{area}] " if area else ""
            lines.append(f"- {prefix}{msg}")
    reminder = "\n".join(lines).strip() or None

    return HookResult(
        decision=decision,
        reason=report.summary if decision == "block" else None,
        hook_specific_output=PostStageOutput(additional_context=reminder),
    )


__all__ = [
    "AggregatedHookResult",
    "HookResult",
    "PostStageOutput",
    "aggregate",
    "critique_to_hook_result",
]
