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
    Figure,
    ModelSpec,
    ProblemInput,
    ReasoningEffort,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_worker.agents.base import AgentError, AgentParseError
from agent_worker.chart_catalog import render_index_markdown
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.kernel import KernelSession
from agent_worker.prompts import load_prompt

# Iteration budget for the Coder loop. Originally 3 (MVP), bumped to 7 so the
# agent can produce multiple figures across turns rather than cramming
# everything into a single cell. Award-level papers typically need 8-15
# figures; with 7 turns the Coder can cover: baseline, sensitivity tornado,
# heatmap, convergence, Monte Carlo, Pareto/objective, and residuals.
MAX_ITERATIONS = 7


class FigureMeta(BaseModel):
    """Per-cell figure registration — the Coder emits one entry per savefig."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    caption: str
    width: float = Field(default=0.8, gt=0.0, le=1.0)


class CoderDirective(BaseModel):
    """Structured output the LLM must return on each turn."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str
    code: str
    done: bool
    summary: str | None = None
    # Figures the LLM claims to have saved in THIS cell. The pipeline trusts
    # these IDs and only verifies the PNG exists on disk; missing files are
    # dropped with a warning so one bad savefig doesn't kill the run.
    figures_saved: list[FigureMeta] = Field(default_factory=list)


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
        run_effort: ReasoningEffort = "medium",
        long_context: bool = False,
        model_override: str | None = None,
    ) -> None:
        self.gateway = gateway
        self.emitter = emitter
        self.kernel = kernel
        self.prompt = load_prompt(self.AGENT_NAME, prompt_version)
        self._run_effort: ReasoningEffort = run_effort
        self._long_context: bool = long_context
        self._model_override: str | None = model_override

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
        figures: list[Figure] = []
        seen_ids: set[str] = set()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {
                "role": "user",
                "content": self.prompt.render_user(
                    problem_text=problem.problem_text,
                    analysis_json=json.dumps(
                        analysis.model_dump(mode="json"), ensure_ascii=False, indent=2
                    ),
                    # Trimmed spec — Coder only needs what it implements. The
                    # full ModelSpec (rationale / complexity_notes /
                    # consulted_methods) goes to the Writer instead. In prod,
                    # observed 10k-char specs can push Coder input past the
                    # upstream's context limit and trigger empty 200 OK responses.
                    spec_json=json.dumps(
                        _trim_spec_for_coder(spec), ensure_ascii=False, indent=2
                    ),
                    # Catalog index: id + name + when-to-use + primary pitfall.
                    # Snippets are NOT injected — token budget would blow up
                    # past ~15k otherwise. LLM references catalog entries by id.
                    chart_catalog_index=render_index_markdown(),
                ),
            },
        ]
        model = self._model_override or self.prompt.model_preference[0]
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
                # Register figures the LLM claims to have saved, guarded by
                # on-disk existence: LLMs occasionally hallucinate a savefig.
                # The Writer will only ever see figures we can actually ship.
                for fm in directive.figures_saved:
                    if fm.id in seen_ids:
                        continue
                    png_rel = f"figures/{fm.id}.png"
                    svg_rel = f"figures/{fm.id}.svg"
                    png_abs = self.kernel.run_dir / png_rel
                    if not png_abs.is_file():
                        await self.emitter.emit(
                            "log",
                            {
                                "level": "warn",
                                "message": (
                                    f"figure {fm.id!r} declared but PNG not "
                                    f"found at {png_rel}; skipping"
                                ),
                            },
                            agent=self.AGENT_NAME,
                        )
                        continue
                    svg_abs = self.kernel.run_dir / svg_rel
                    figures.append(
                        Figure(
                            id=fm.id,
                            caption=fm.caption,
                            path_png=png_rel,
                            path_svg=svg_rel if svg_abs.is_file() else None,
                            width=fm.width,
                        )
                    )
                    seen_ids.add(fm.id)

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
            # Back-compat: `figure_paths` is the union of inline-display paths
            # captured by the kernel AND the explicit PNG paths the LLM
            # registered. Older consumers that only read `figure_paths` still
            # see every figure we shipped.
            all_figures = [p for c in cells for p in c.figure_paths]
            for fig in figures:
                if fig.path_png not in all_figures:
                    all_figures.append(fig.path_png)

            output = CoderOutput(
                cells=cells,
                figures=figures,
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
                            "reasoning, code, done, summary, figures_saved."
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
        # Reuse the shared retry helper so Coder gets the same empty-response
        # and transport-error resilience as BaseAgent subclasses.
        from agent_worker.agents.base import _stream_with_retry

        return await _stream_with_retry(
            gateway=self.gateway,
            emitter=self.emitter,
            agent_name=self.AGENT_NAME,
            model=model,
            messages=messages,
            temperature=self.prompt.temperature,
            max_tokens=1_000_000 if self._long_context else 20000,
            response_format={"type": "json_object"},
            reasoning_effort=self.prompt.reasoning_effort or self._run_effort,
        )

    async def _stream_and_collect_raw(
        self, model: str, messages: list[dict[str, Any]]
    ) -> str:
        # Kept for any future need — currently unused. The original inline
        # implementation is left below for reference / diff compactness.
        effort = self.prompt.reasoning_effort or self._run_effort
        parts: list[str] = []
        async for delta in self.gateway.stream_completion(
            run_id=self.emitter.run_id,
            agent=self.AGENT_NAME,
            model=model,
            messages=messages,
            temperature=self.prompt.temperature,
            # 20k default, 1M when long-context opt-in is set. See base.py.
            max_tokens=1_000_000 if self._long_context else 20000,
            response_format={"type": "json_object"},
            reasoning_effort=effort,
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
    def _render_execution_feedback(cell: CellExecution) -> str:  # noqa: D401
        # See comment below in _trim_spec_for_coder for context.
        return _render_feedback(cell)


def _trim_spec_for_coder(spec: ModelSpec) -> dict[str, Any]:
    """Return a trimmed `ModelSpec` dict containing only what the Coder needs.

    The Coder implements the model. It doesn't need the Modeler's prose
    justification for awards-level output (rationale, complexity notes,
    HMML consultation trail) — those are for the Writer. Passing the full
    spec (often 8-12k chars post-award-mode prompt) risks pushing Coder's
    total input past the upstream context window, which has been observed
    to manifest as silent empty 200 OK responses on OpenAI-compat proxies.
    """
    d = spec.model_dump(mode="json")
    trimmed: dict[str, Any] = {
        "chosen_approach": d.get("chosen_approach", ""),
        "variables": d.get("variables", []),
        "equations": d.get("equations", []),
        "algorithm_outline": d.get("algorithm_outline", []),
        "validation_strategy": d.get("validation_strategy", ""),
    }
    return trimmed


def _render_feedback(cell: CellExecution) -> str:
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


__all__ = ["CoderAgent", "CoderDirective", "FigureMeta", "MAX_ITERATIONS"]
