"""PaperEditorAgent: tool-using fine-tune loop for a generated paper.

This agent is purely interactive (driven by a user's natural-language
instruction) and lives ENTIRELY outside the main pipeline. It does not
inherit from `BaseAgent` because its lifecycle is different:

* It loads paper.meta.json + notebook.ipynb from `runs/<run_id>/` rather
  than receiving structured upstream output.
* It runs an iterative tool-using loop (similar to `CoderAgent`), bounded
  by `max_iterations` (default 8).
* The LLM emits a JSON directive picking ONE tool per turn; we execute the
  tool, feed the result back, and repeat until `done=true` or the budget
  is exhausted.

Event contract mirrors `CoderAgent`:
    stage.start -> finetune.tool_call -> finetune.tool_result -> stage.done
Errors bubble out as `AgentError` for the caller (the gateway-facing
HTTP handler) to translate into an error response.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

import orjson
from mm_contracts import ReasoningEffort
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_worker.agents.base import AgentError, AgentParseError, _stream_with_retry
from agent_worker.editor_tools import ToolContext, build_tool_registry
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.kernel import KernelSession
from agent_worker.prompts import load_prompt

MAX_ITERATIONS = 8

# Allowed `tool` values — kept in sync with the prompt's tool catalogue.
ToolName = Literal[
    "read_paper",
    "edit_section",
    "surgical_edit",
    "edit_constant",
    "run_cell",
    "regenerate_figure",
    "recompile_pdf",
]


class PaperEditorDirective(BaseModel):
    """Structured output the LLM must emit on each turn."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str
    tool: ToolName
    args: dict[str, Any]
    done: bool = False
    summary: str | None = None


class PaperEditorAgent:
    """Run a bounded tool-using loop that fine-tunes a generated paper.

    Pattern mirrors `CoderAgent`:
      messages = [system, user(instruction + outline + last_tool_result)]
      for _ in range(max_iterations):
          directive = _ask_llm(messages)
          result = registry[directive.tool].execute(directive.args, ctx)
          if directive.done: return directive.summary
          messages.append(assistant=directive, user=result_feedback)
    """

    AGENT_NAME = "paper_editor"

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        kernel: KernelSession | None,
        run_dir: Path,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "high",
        model_override: str | None = None,
    ) -> None:
        self.gateway = gateway
        self.emitter = emitter
        self.kernel = kernel
        self.run_dir = Path(run_dir)
        self.prompt = load_prompt(self.AGENT_NAME, prompt_version)
        self._run_effort: ReasoningEffort = run_effort
        self._model_override: str | None = model_override
        self._registry = build_tool_registry()

    async def fine_tune(
        self,
        user_message: str,
        run_dir: Path | None = None,
        max_iterations: int = MAX_ITERATIONS,
    ) -> str:
        """Drive the iterative loop until `done=true` or the budget is exhausted.

        Returns a one-line summary string suitable for the gateway response.
        """
        if run_dir is not None:
            self.run_dir = Path(run_dir)
        t0 = time.monotonic()
        await self.emitter.emit(
            "stage.start", {"stage": self.AGENT_NAME}, agent=self.AGENT_NAME
        )

        ctx = self._load_context()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {
                "role": "user",
                "content": self.prompt.render_user(
                    user_message=user_message,
                    paper_outline=self._render_outline(ctx),
                    last_tool_result="(none — this is the first turn)",
                ),
            },
        ]
        model = self._model_override or self.prompt.model_preference[0]

        final_summary: str | None = None
        try:
            for _ in range(max_iterations):
                directive = await self._ask_llm(model, messages)
                tool = self._registry.get(directive.tool)
                if tool is None:
                    # The Pydantic schema already constrains `tool` to the
                    # Literal set, so reaching here means the schema and
                    # registry drifted — which is a bug, not user-recoverable.
                    raise AgentError(
                        f"PaperEditor produced unknown tool name {directive.tool!r}"
                    )

                await self.emitter.emit(
                    "finetune.tool_call",
                    {
                        "tool": directive.tool,
                        "args": directive.args,
                        "reasoning": directive.reasoning,
                    },
                    agent=self.AGENT_NAME,
                )
                result = await tool.execute(directive.args, ctx)
                await self.emitter.emit(
                    "finetune.tool_result",
                    {
                        "tool": directive.tool,
                        "ok": result.ok,
                        "summary": result.summary,
                        "error": result.error,
                    },
                    agent=self.AGENT_NAME,
                )

                if directive.done:
                    final_summary = directive.summary or "(no summary provided)"
                    break

                # Feed directive + result back; this drives the next turn.
                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(directive.model_dump(mode="json")),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": self._render_tool_feedback(ctx, result),
                    }
                )

            if final_summary is None:
                final_summary = (
                    f"Reached iteration limit ({max_iterations}) without "
                    "the model setting done=true."
                )

            duration_ms = int((time.monotonic() - t0) * 1000)
            await self.emitter.emit(
                "stage.done",
                {
                    "stage": self.AGENT_NAME,
                    "duration_ms": duration_ms,
                    "summary": final_summary,
                },
                agent=self.AGENT_NAME,
            )
            return final_summary
        except AgentError:
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self.emitter.emit(
                "stage.done",
                {
                    "stage": self.AGENT_NAME,
                    "duration_ms": duration_ms,
                    "status": "failed",
                },
                agent=self.AGENT_NAME,
            )
            raise

    # ------------------------------------------------------------------ context

    def _load_context(self) -> ToolContext:
        """Read paper.meta.json + notebook.ipynb into a fresh ToolContext.

        Missing files are tolerated with empty dicts — newly created runs
        may not have all artifacts yet, and the LLM can still call read_paper
        to see the (empty) outline and decide to bail.
        """
        meta_path = self.run_dir / "paper.meta.json"
        nb_path = self.run_dir / "notebook.ipynb"

        paper_meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                paper_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                paper_meta = {}

        notebook: dict[str, Any] = {"cells": []}
        next_cell_index = 0
        if nb_path.is_file():
            try:
                notebook = json.loads(nb_path.read_text(encoding="utf-8"))
                # Continue numbering after the highest existing execution_count
                # so newly-appended cells don't collide with existing ones in
                # the rendered notebook viewer.
                existing = [
                    int(c.get("execution_count") or 0)
                    for c in (notebook.get("cells") or [])
                    if c.get("cell_type") == "code"
                ]
                if existing:
                    next_cell_index = max(existing)
            except (json.JSONDecodeError, OSError):
                notebook = {"cells": []}

        return ToolContext(
            run_dir=self.run_dir,
            paper_meta=paper_meta,
            notebook=notebook,
            kernel=self.kernel,
            gateway=self.gateway,
            emitter=self.emitter,
            next_cell_index=next_cell_index,
        )

    @staticmethod
    def _render_outline(ctx: ToolContext) -> str:
        """One-line-per-section outline for the system prompt's first turn."""
        meta = ctx.paper_meta
        lines: list[str] = []
        title = meta.get("title")
        if title:
            lines.append(f"title: {title}")
        abstract = meta.get("abstract") or ""
        if abstract:
            lines.append(
                f"abstract ({len(abstract)} chars): {abstract[:120].replace(chr(10), ' ')}..."
            )
        for sec in meta.get("sections", []) or []:
            sec_title = sec.get("title", "")
            body = sec.get("body_markdown", "") or ""
            lines.append(
                f"- {sec_title} ({len(body)} chars): "
                f"{body[:120].replace(chr(10), ' ')}..."
            )
        if not lines:
            return "(no paper.meta.json found — paper not yet generated)"
        return "\n".join(lines)

    @staticmethod
    def _render_tool_feedback(ctx: ToolContext, result: Any) -> str:
        """Format the tool result for the LLM's next turn.

        We keep this compact: the LLM mostly needs the `summary` line; the
        full `detail` is only included for read_paper, where the LLM
        explicitly asked for the section body.
        """
        parts = [f"Tool result ok={result.ok}: {result.summary}"]
        if result.error:
            parts.append(f"error: {result.error}")
        if result.detail:
            detail = result.detail
            if len(detail) > 4000:
                detail = detail[:4000] + "\n... <truncated>"
            parts.append(f"detail:\n{detail}")
        parts.append("Continue with another tool, or set done=true with a summary.")
        return "\n".join(parts)

    # ------------------------------------------------------------------ llm

    async def _ask_llm(
        self, model: str, messages: list[dict[str, Any]]
    ) -> PaperEditorDirective:
        """Stream one completion and parse it into a PaperEditorDirective.

        Mirrors `CoderAgent._ask_llm`: one retry on parse failure, then
        fail loudly so the gateway returns 5xx and the user can retry.
        """
        attempts = 0
        last_err: Exception | None = None
        local_messages = list(messages)
        while attempts < 2:
            attempts += 1
            text = await self._stream_and_collect(model, local_messages)
            try:
                return self._parse_directive(text)
            except AgentParseError as e:
                last_err = e
                local_messages = [
                    *local_messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response could not be parsed as a "
                            "PaperEditorDirective JSON object. Error: "
                            f"{e}. Return ONLY a JSON object with keys "
                            "reasoning, tool, args, done, summary."
                        ),
                    },
                ]

        await self.emitter.emit(
            "error",
            {
                "message": f"{self.AGENT_NAME} parse failed: {last_err}",
                "code": "parse_failed",
                "stage": self.AGENT_NAME,
            },
            agent=self.AGENT_NAME,
        )
        raise AgentError(
            f"{self.AGENT_NAME} produced unparseable output"
        ) from last_err

    async def _stream_and_collect(
        self, model: str, messages: list[dict[str, Any]]
    ) -> str:
        return await _stream_with_retry(
            gateway=self.gateway,
            emitter=self.emitter,
            agent_name=self.AGENT_NAME,
            model=model,
            messages=messages,
            temperature=self.prompt.temperature,
            max_tokens=self.prompt.token_budget_out or 4000,
            response_format={"type": "json_object"},
            reasoning_effort=self.prompt.reasoning_effort or self._run_effort,
        )

    @staticmethod
    def _parse_directive(text: str) -> PaperEditorDirective:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()
        try:
            obj = orjson.loads(cleaned)
        except Exception as e:  # noqa: BLE001 — orjson raises a generic Exception
            raise AgentParseError(f"not valid JSON: {e}") from e
        try:
            return PaperEditorDirective.model_validate(obj)
        except ValidationError as e:
            raise AgentParseError(str(e)) from e


__all__ = [
    "MAX_ITERATIONS",
    "PaperEditorAgent",
    "PaperEditorDirective",
]
