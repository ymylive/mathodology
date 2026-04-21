"""Shared agent lifecycle: load prompt, stream LLM, accumulate, parse JSON.

Subclasses only need to declare `AGENT_NAME` and `OUTPUT_MODEL` and supply
template variables via `run(**vars)` (or wrap that with a typed helper).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import orjson
from mm_contracts import ReasoningEffort
from pydantic import BaseModel, ValidationError

from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.prompts import load_prompt


class AgentError(Exception):
    """Base class for agent failures that should surface to the pipeline."""


class AgentParseError(AgentError):
    """Raised when an LLM response can't be parsed into OUTPUT_MODEL."""


class BaseAgent:
    """Common lifecycle for every LLM-backed agent."""

    AGENT_NAME: ClassVar[str]
    OUTPUT_MODEL: ClassVar[type[BaseModel]]

    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "medium",
        long_context: bool = False,
    ) -> None:
        self.gateway = gateway
        self.emitter = emitter
        self.prompt = load_prompt(self.AGENT_NAME, prompt_version)
        # Run-level reasoning effort from `ProblemInput.reasoning_effort`.
        # Individual prompt specs may override via `PromptSpec.reasoning_effort`.
        self._run_effort: ReasoningEffort = run_effort
        self._long_context: bool = long_context

    async def run(self, **template_vars: Any) -> BaseModel:
        """Emit stage.start, call the LLM (with one parse-retry), emit output + stage.done."""
        t0 = time.monotonic()
        await self.emitter.emit(
            "stage.start", {"stage": self.AGENT_NAME}, agent=self.AGENT_NAME
        )

        model = self.prompt.model_preference[0]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {"role": "user", "content": self.prompt.render_user(**template_vars)},
        ]

        # Retry up to 2 times on parse failure (HTTP errors bubble up unchanged).
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                text = await self._stream_and_collect(model, messages)
                output = self._parse_output(text)
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self.emitter.emit(
                    "agent.output",
                    {
                        "schema_name": self.OUTPUT_MODEL.__name__,
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
            except AgentParseError as e:
                last_err = e
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response could not be parsed as JSON "
                            f"matching {self.OUTPUT_MODEL.__name__}. Error: {e}. "
                            f"Return ONLY a valid JSON object this time."
                        ),
                    }
                )

        await self.emitter.emit(
            "error",
            {
                "message": f"{self.AGENT_NAME} failed after 2 attempts: {last_err}",
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
        """Consume the gateway's SSE stream and concatenate text deltas."""
        effort = self.prompt.reasoning_effort or self._run_effort
        parts: list[str] = []
        async for delta in self.gateway.stream_completion(
            run_id=self.emitter.run_id,
            agent=self.AGENT_NAME,
            model=model,
            messages=messages,
            temperature=self.prompt.temperature,
            # 20k default, 1M when the user opts into long context (only
            # sensible on models that advertise 1M — Claude 3.5 Sonnet 1M
            # beta, Gemini 2.0, gpt-5-1m). Un-capping (`None`) makes some
            # OpenAI-compat proxies fall back to a 1k-4k default; an
            # explicit cap avoids that trap.
            max_tokens=1_000_000 if self._long_context else 20000,
            response_format={"type": "json_object"},
            reasoning_effort=effort,
        ):
            parts.append(delta)
        return "".join(parts)

    def _parse_output(self, text: str) -> BaseModel:
        """Strip markdown fences, parse JSON, validate against OUTPUT_MODEL."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Drop the opening fence line (``` or ```json etc.)
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()

        try:
            obj = orjson.loads(cleaned)
        except Exception as e:
            raise AgentParseError(f"not valid JSON: {e}") from e

        try:
            return self.OUTPUT_MODEL.model_validate(obj)
        except ValidationError as e:
            raise AgentParseError(str(e)) from e


__all__ = ["AgentError", "AgentParseError", "BaseAgent"]
