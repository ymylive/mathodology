"""Coder agent: iterative code-exec loop around a Jupyter kernel.

Unlike the Analyzer (single-shot LLM → parse), the Coder loops up to
`MAX_ITERATIONS` times around *LLM → execute → feedback → LLM*. The kernel
lifetime is managed by the caller so execution state persists across turns.

Event contract matches `BaseAgent`:
    stage.start → (kernel.stdout / kernel.figure per cell) → agent.output → stage.done
and on failure: `error` followed by an AgentError bubbling to the pipeline.
"""

from __future__ import annotations

import json
import time
from typing import Any

import orjson
from mm_contracts import (
    AnalyzerOutput,
    CellExecution,
    CoderOutput,
    ModelSpec,
    ProblemInput,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_worker.agents.base import AgentError, AgentParseError
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.kernel import KernelSession
from agent_worker.prompts import load_prompt

MAX_ITERATIONS = 3


class CoderDirective(BaseModel):
    """Structured output the LLM must return on each turn."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str
    code: str
    done: bool
    summary: str | None = None


class CoderAgent:
    """Run an iterative code-exec loop, ending in a notebook + summary."""

    AGENT_NAME = "coder"
    OUTPUT_MODEL = CoderOutput

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        kernel: KernelSession,
        prompt_version: str = "v1",
    ) -> None:
        self.gateway = gateway
        self.emitter = emitter
        self.kernel = kernel
        self.prompt = load_prompt(self.AGENT_NAME, prompt_version)

    async def run(
        self,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        spec: ModelSpec,
    ) -> CoderOutput:
        """Execute the agent loop end-to-end; always emits stage lifecycle events."""
        t0 = time.monotonic()
        await self.emitter.emit(
            "stage.start", {"stage": self.AGENT_NAME}, agent=self.AGENT_NAME
        )

        await self.kernel.start()
        cells: list[CellExecution] = []
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {
                "role": "user",
                "content": self.prompt.render_user(
                    problem_text=problem.problem_text,
                    analysis_json=json.dumps(
                        analysis.model_dump(mode="json"), ensure_ascii=False, indent=2
                    ),
                    spec_json=json.dumps(
                        spec.model_dump(mode="json"), ensure_ascii=False, indent=2
                    ),
                ),
            },
        ]
        model = self.prompt.model_preference[0]
        final_summary: str | None = None

        try:
            for i in range(MAX_ITERATIONS):
                directive = await self._ask_llm(model, messages)

                await self.emitter.emit(
                    "log",
                    {"level": "info", "message": f"executing cell {i}"},
                    agent=self.AGENT_NAME,
                )
                cell = await self.kernel.execute(
                    directive.code, cell_index=i, emitter=self.emitter
                )
                cells.append(cell)

                if directive.done:
                    final_summary = directive.summary or "(no summary provided)"
                    break
                if cell.error and i == MAX_ITERATIONS - 1:
                    final_summary = (
                        f"Coder failed after {MAX_ITERATIONS} attempts: {cell.error}"
                    )
                    break

                # Feed the directive and execution result back for the next turn.
                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(directive.model_dump(mode="json")),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": self._render_execution_feedback(cell),
                    }
                )
            if final_summary is None:
                final_summary = "Reached iteration limit without explicit done."

            notebook_path = await self.kernel.write_notebook(cells)
            all_figures = [p for c in cells for p in c.figure_paths]

            output = CoderOutput(
                cells=cells,
                figure_paths=all_figures,
                final_summary=final_summary,
                notebook_path=str(notebook_path),
            )

            duration_ms = int((time.monotonic() - t0) * 1000)
            await self.emitter.emit(
                "agent.output",
                {
                    "schema_name": "CoderOutput",
                    "output": output.model_dump(mode="json"),
                    "duration_ms": duration_ms,
                },
                agent=self.AGENT_NAME,
            )
            await self.emitter.emit(
                "stage.done",
                {"stage": self.AGENT_NAME, "duration_ms": duration_ms},
                agent=self.AGENT_NAME,
            )
            return output
        finally:
            await self.kernel.shutdown()

    # ------------------------------------------------------------------ helpers

    async def _ask_llm(
        self, model: str, messages: list[dict[str, Any]]
    ) -> CoderDirective:
        """Stream a completion, parse JSON → CoderDirective; retry once on failure."""
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
                            "CoderDirective JSON object. Error: "
                            f"{e}. Return ONLY a JSON object with keys "
                            "reasoning, code, done, summary."
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
        parts: list[str] = []
        async for delta in self.gateway.stream_completion(
            run_id=self.emitter.run_id,
            agent=self.AGENT_NAME,
            model=model,
            messages=messages,
            temperature=self.prompt.temperature,
            max_tokens=self.prompt.token_budget_out,
            response_format={"type": "json_object"},
        ):
            parts.append(delta)
        return "".join(parts)

    @staticmethod
    def _parse_directive(text: str) -> CoderDirective:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()
        try:
            obj = orjson.loads(cleaned)
        except Exception as e:
            raise AgentParseError(f"not valid JSON: {e}") from e
        try:
            return CoderDirective.model_validate(obj)
        except ValidationError as e:
            raise AgentParseError(str(e)) from e

    @staticmethod
    def _render_execution_feedback(cell: CellExecution) -> str:
        parts = [
            f"Cell {cell.index} executed in {cell.duration_ms}ms.",
            f"stdout: {cell.stdout!r}" if cell.stdout else "stdout: (empty)",
            f"stderr: {cell.stderr!r}" if cell.stderr else "stderr: (empty)",
            (
                f"result: {cell.result_text!r}"
                if cell.result_text is not None
                else "result: (none)"
            ),
            f"error: {cell.error}" if cell.error else "error: (none)",
            (
                f"figures saved: {cell.figure_paths}"
                if cell.figure_paths
                else "figures saved: []"
            ),
            "Continue with another cell, or set done=true and provide a summary.",
        ]
        return "\n".join(parts)


__all__ = ["CoderAgent", "CoderDirective", "MAX_ITERATIONS"]
