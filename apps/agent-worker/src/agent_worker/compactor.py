"""Shared text-compaction utility, modeled on claude-code-sourcemap's compact
subsystem.

The pattern:
- Threshold-triggered: only invoke the LLM when input exceeds a budget.
- LLM-summarized: domain-tunable prompts compress while preserving signal.
- Circuit-breaker: 3 consecutive failures → stop calling the LLM until a
  later success resets the counter. (Same `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`
  pattern as sourcemap's `autoCompact.ts:62`.)
- No-tools preamble: tool-trained models like to call tools mid-summary;
  the system prompt rejects that explicitly.

Subclass `Compactor` (override `SYSTEM_PROMPT` and `build_user_prompt`) to
customize per domain — see `PaperCompactor` for the Searcher's academic-paper
flavor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Borrowed verbatim-in-spirit from sourcemap's NO_TOOLS_PREAMBLE: tool-trained
# models occasionally try to call tools mid-summary, which on a single-turn
# completion path means no text output at all. Stating consequences upfront
# prevents that wasted turn. Concatenated onto every subclass SYSTEM_PROMPT.
NO_TOOLS_PREAMBLE: str = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
    "- You already have all the context you need in the user message.\n"
    "- Tool calls will be REJECTED and waste your only turn.\n"
    "- Your entire response must be plain text.\n\n"
)


@dataclass
class CompactionPolicy:
    """Knobs governing when to fire and how much to keep."""

    threshold_chars: int = 24_000
    target_chars: int = 24_000           # aim summary at this size
    min_output_chars: int = 800          # if shorter -> treat as failed
    max_consecutive_failures: int = 3    # circuit breaker
    keep_recent_chars: int = 2_000       # tail kept verbatim (reserved for
                                         #  message-style compaction; unused
                                         #  by the text-only path today)


@dataclass
class CompactionResult:
    """Outcome of a single `Compactor.compact` call.

    `was_compacted` is true iff the LLM was invoked AND produced a usable
    summary. On every other path (below threshold, breaker tripped, LLM error,
    output too short, no actual compression) `compacted` equals the original
    text and `failure_reason` explains why.
    """

    compacted: str
    was_compacted: bool
    original_chars: int
    compacted_chars: int
    failure_reason: str | None = None


class CompactorCaller(Protocol):
    """Async fn the Compactor uses to call the LLM. Returns the assistant text."""

    async def __call__(self, *, system: str, user: str, model: str) -> str: ...


class Compactor:
    """Threshold-triggered, LLM-summarized text compactor with a circuit breaker.

    Subclassable: override `SYSTEM_PROMPT` / `build_user_prompt` for domain-
    specific compaction (e.g. `PaperCompactor` for academic PDFs).
    """

    SYSTEM_PROMPT: str = (
        "You are a precise summarizer. Compress the user-provided text while "
        "preserving all numbers, equations, named entities, and citations. "
        "Do NOT call tools. Return plain text only."
    )

    def __init__(
        self,
        caller: CompactorCaller,
        *,
        model: str = "gpt-5.5",
        policy: CompactionPolicy | None = None,
    ) -> None:
        self._caller = caller
        self._model = model
        self._policy = policy or CompactionPolicy()
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Current breaker count. Exposed for diagnostics and tests."""
        return self._consecutive_failures

    @property
    def policy(self) -> CompactionPolicy:
        return self._policy

    def should_compact(self, text: str) -> bool:
        """Two conditions: input exceeds threshold AND breaker not tripped."""
        return (
            len(text) > self._policy.threshold_chars
            and self._consecutive_failures < self._policy.max_consecutive_failures
        )

    def build_user_prompt(self, text: str) -> str:
        """Override to customize the compaction prompt."""
        return (
            f"Compress the following to at most ~{self._policy.target_chars} "
            f"characters. Preserve all specific numbers and equations.\n\n{text}"
        )

    def _full_system_prompt(self) -> str:
        """SYSTEM_PROMPT with the NO_TOOLS_PREAMBLE prepended."""
        return NO_TOOLS_PREAMBLE + self.SYSTEM_PROMPT

    async def compact(self, text: str) -> CompactionResult:
        original = len(text)
        if not self.should_compact(text):
            reason = (
                "below_threshold"
                if original <= self._policy.threshold_chars
                else "circuit_breaker_tripped"
            )
            return CompactionResult(
                compacted=text,
                was_compacted=False,
                original_chars=original,
                compacted_chars=original,
                failure_reason=reason,
            )
        try:
            user_prompt = self.build_user_prompt(text)
            summary = await self._caller(
                system=self._full_system_prompt(),
                user=user_prompt,
                model=self._model,
            )
        except Exception as exc:  # noqa: BLE001 — any caller error is a failure
            self._consecutive_failures += 1
            return CompactionResult(
                compacted=text,
                was_compacted=False,
                original_chars=original,
                compacted_chars=original,
                failure_reason=f"llm_error: {type(exc).__name__}: {exc}",
            )
        # Normalize None / non-string returns to empty so the length checks
        # treat them as "too short". Callers SHOULD return str, but the
        # gateway path historically tolerated odd shapes.
        if not isinstance(summary, str):
            summary = ""
        summary = summary.strip()
        if len(summary) < self._policy.min_output_chars:
            self._consecutive_failures += 1
            return CompactionResult(
                compacted=text,
                was_compacted=False,
                original_chars=original,
                compacted_chars=original,
                failure_reason="output_too_short",
            )
        if len(summary) >= original:
            # LLM returned same-or-larger; not compressing — keep original.
            self._consecutive_failures += 1
            return CompactionResult(
                compacted=text,
                was_compacted=False,
                original_chars=original,
                compacted_chars=original,
                failure_reason="no_compression_achieved",
            )
        # Success — reset breaker.
        self._consecutive_failures = 0
        return CompactionResult(
            compacted=summary,
            was_compacted=True,
            original_chars=original,
            compacted_chars=len(summary),
        )


class PaperCompactor(Compactor):
    """Domain-specific subclass for academic paper text (Searcher's use case).

    Preserves the exact behavioral envelope of the inline implementation that
    used to live in `searcher.py`: same 24k threshold, same prompt intent
    (preserve numbers / methods / DOIs; drop boilerplate / refs), same
    source-language preservation rule.
    """

    SYSTEM_PROMPT = (
        "You compress an academic paper into a high-density summary for "
        "downstream LLM citation. Preserve verbatim:\n"
        "- Mathematical formulas (LaTeX or plain)\n"
        "- Parameter definitions and units\n"
        "- Methodology steps in order\n"
        "- Experimental setup (sample size, data source, conditions)\n"
        "- Numerical results with confidence intervals or error bars\n"
        "- Key claims that Writer might cite\n\n"
        "Drop entirely:\n"
        "- Boilerplate (acknowledgments, ethics, conflicts of interest)\n"
        "- Related-work prose (the Writer has SearchFindings.papers for that)\n"
        "- Narrative filler (\"In this paper we propose...\")\n"
        "- Reference list (keep DOIs only)\n"
        "- Repeated information\n\n"
        "Preserve the source language. If the input is Chinese, output Chinese.\n"
        "If English, output English. Do not translate.\n\n"
        "Output: dense markdown, capped at the target character budget. Keep "
        "section headings (## Methods, ## Results, etc.) so Writer can grep "
        "for them. Respond with the compacted markdown only."
    )

    def build_user_prompt(self, text: str) -> str:
        return (
            f"Compact this paper text into ~{self._policy.target_chars} "
            f"characters of dense markdown, preserving the source language "
            f"verbatim and keeping all numerical results, methods detail, "
            f"and DOIs.\n\nRaw paper text:\n\n{text}"
        )


__all__ = [
    "NO_TOOLS_PREAMBLE",
    "CompactionPolicy",
    "CompactionResult",
    "Compactor",
    "CompactorCaller",
    "PaperCompactor",
]
