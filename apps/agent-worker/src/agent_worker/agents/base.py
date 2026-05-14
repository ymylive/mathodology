"""Shared agent lifecycle: load prompt, stream LLM, accumulate, parse JSON.

Subclasses only need to declare `AGENT_NAME` and `OUTPUT_MODEL` and supply
template variables via `run(**vars)` (or wrap that with a typed helper).
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

import orjson
from mm_contracts import CritiqueReport, ReasoningEffort
from pydantic import BaseModel, ValidationError

from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.prompts import load_prompt
from agent_worker.skills import SkillRegistry, SkillTool


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
        run_effort: ReasoningEffort = "high",
        long_context: bool = False,
        model_override: str | None = None,
        skill_registry: SkillRegistry | None = None,
        use_skill_tool: bool = False,
    ) -> None:
        self.gateway = gateway
        self.emitter = emitter
        self.prompt = load_prompt(self.AGENT_NAME, prompt_version)
        # Run-level reasoning effort from `ProblemInput.reasoning_effort`.
        # Individual prompt specs may override via `PromptSpec.reasoning_effort`.
        self._run_effort: ReasoningEffort = run_effort
        self._long_context: bool = long_context
        # User-selected model from SettingsPanel, threaded via
        # `ProblemInput.model_override`. When set, it wins over the TOML's
        # `model_preference[0]` for every agent in the run.
        self._model_override: str | None = model_override
        # On-demand skill loading. When `use_skill_tool=True` and the
        # registry has at least one entry, the system prompt carries the
        # menu (frontmatter only) and the `get_skill` tool. Default off
        # so existing eager-load behavior is preserved for every agent
        # until we have signal that the tool path is safe.
        self._skill_registry: SkillRegistry | None = skill_registry
        self._use_skill_tool: bool = use_skill_tool
        self._skill_tool: SkillTool | None = (
            SkillTool(skill_registry)
            if (use_skill_tool and skill_registry is not None and len(skill_registry) > 0)
            else None
        )

    def _system_prompt_text(self) -> str:
        """System prompt text with the on-demand skill menu optionally appended.

        Kept as a method so subclasses (and `Coder`, which doesn't inherit
        but mirrors the same shape) can share the assembly rule without
        each one re-implementing the conditional. The menu is appended
        AFTER the original system text so existing prompt-cache breakpoints
        still align with the same prefix when the flag is off.
        """
        base = self.prompt.system["text"]
        if self._skill_tool is None or self._skill_registry is None:
            return base
        menu = self._skill_registry.render_menu()
        if not menu:
            return base
        return f"{base}\n\n{menu}"

    @property
    def skill_tool(self) -> SkillTool | None:
        """The configured ``SkillTool`` instance, or ``None`` when disabled."""
        return self._skill_tool

    async def run(self, **template_vars: Any) -> BaseModel:
        """Emit stage.start, call the LLM (with one parse-retry), emit output + stage.done."""
        t0 = time.monotonic()
        await self.emitter.emit(
            "stage.start", {"stage": self.AGENT_NAME}, agent=self.AGENT_NAME
        )

        model = self._model_override or self.prompt.model_preference[0]
        # `cache_breakpoint=True` on the system message tells the gateway to
        # emit Anthropic-style `cache_control: {type: "ephemeral"}` on that
        # block. Anthropic caches the prefix up to (and including) the
        # breakpoint, so the entire system prompt (the BIG stable chunk —
        # role, schemas, chart catalog, few-shot exemplars for Coder/Writer)
        # becomes eligible for prompt-cache hits on every repeated call.
        # User messages vary per turn and are intentionally left unmarked.
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._system_prompt_text(),
                "cache_breakpoint": True,
            },
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
        """Consume the gateway's SSE stream and concatenate text deltas.

        Robust against two observed upstream flakiness modes:
        1. Transport disconnects mid-stream (`RemoteProtocolError` etc.)
        2. Silent empty 200 OK: stream completes with zero content deltas.
           Observed with cornna/gpt-5.4 — appears randomly across agents.
        Both are retried with exponential backoff (5s, 15s, 30s between
        retries, up to 4 attempts total).
        """
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

    def _parse_output(self, text: str) -> BaseModel:
        """Strip markdown fences, parse JSON (lenient), validate against OUTPUT_MODEL.

        Two robustness tricks borrowed from claude-code-sourcemap
        (`utils/json.ts`, `utils/toolErrors.ts`):

        1. **Lenient JSON parse via raw_decode prefix recovery.** Long
           generations sometimes append trailing text after the JSON object
           (e.g. ``"…}\n\nNote: the above…"``). Strict `orjson.loads`
           rejects the whole blob; the stdlib `json.JSONDecoder.raw_decode`
           cleanly extracts the prefix and we discard the tail.
        2. **LLM-friendly Pydantic error rewrite.** Raw Pydantic
           `ValidationError.__str__()` is verbose URL-laden prose; the
           rewritten format mirrors sourcemap's `formatZodValidationError`
           and dramatically lifts retry success rates by listing concrete
           "missing field X" / "unexpected field Y" bullets.
        """
        cleaned = _strip_json_fence(text)
        obj = _parse_json_lenient(cleaned)
        try:
            return self.OUTPUT_MODEL.model_validate(obj)
        except ValidationError as e:
            raise AgentParseError(_format_validation_error(e)) from e

    async def revise_with_critique(
        self,
        *,
        original_output: BaseModel,
        critique: CritiqueReport,
        context: dict[str, Any],
    ) -> BaseModel:
        """Ask the producing agent to revise its own structured output.

        Mirrors `run()`'s parse-retry-once policy: long-generation runs on
        some models (e.g. gpt-5.5) occasionally emit token-level corruption
        in the JSON stream. Without retry, a single bad reroll fails the
        whole pipeline. We do one retry with the parse error appended.
        """
        model = self._model_override or self.prompt.model_preference[0]
        # Same caching rationale as `run()`: the system prompt is the stable
        # prefix shared across the original generation AND the revision call,
        # so marking it as a breakpoint allows the second call to hit cache.
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._system_prompt_text(),
                "cache_breakpoint": True,
            },
            {
                "role": "user",
                "content": (
                    "Revise your previous JSON output using the Critic feedback below.\n"
                    "Return ONLY a valid JSON object matching "
                    f"{self.OUTPUT_MODEL.__name__}. Preserve correct content; "
                    "change only what is needed to satisfy the critique.\n\n"
                    "Context JSON:\n"
                    f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
                    "Original output JSON:\n"
                    f"{original_output.model_dump_json(indent=2)}\n\n"
                    "Critique JSON:\n"
                    f"{critique.model_dump_json(indent=2)}"
                ),
            },
        ]
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                text = await self._stream_and_collect(model, messages)
                return self._parse_output(text)
            except AgentParseError as e:
                last_err = e
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your previous revision could not be parsed as JSON "
                            f"matching {self.OUTPUT_MODEL.__name__}. Error: {e}. "
                            f"Return ONLY a valid JSON object this time, "
                            f"with all field names quoted correctly and no token-level corruption."
                        ),
                    }
                )
        raise AgentError(
            f"{self.AGENT_NAME} revision produced unparseable output after 2 attempts"
        ) from last_err


async def _stream_with_retry(
    *,
    gateway: GatewayClient,
    emitter: EventEmitter,
    agent_name: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
    reasoning_effort: ReasoningEffort | None,
) -> str:
    """Stream + collect with retries against upstream flakiness.

    Handles three failure modes uniformly:
    - Transport exceptions from httpx (RemoteProtocolError / ReadTimeout /
      ConnectError / PoolTimeout)
    - Empty stream completion: 200 OK with zero content deltas (seen on
      cornna/gpt-5.4 intermittently)
    - HTTP 5xx from the gateway (upstream provider timeout / network error
      surfaces as 502/503/504; we treat these as transient and retry)

    Backoff schedule: 0s, 5s, 15s, 30s before attempts 1..4. Shared by
    BaseAgent subclasses and CoderAgent (which does not inherit BaseAgent).
    """
    import asyncio

    import httpx

    # Wait time BEFORE attempt N (index 0 = attempt 1 = no wait).
    BACKOFFS = [0, 5, 15, 30]
    last_err: Exception | None = None
    for attempt, delay in enumerate(BACKOFFS, start=1):
        if delay > 0:
            await emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": (
                        f"stream retry attempt {attempt}/{len(BACKOFFS)} "
                        f"after {delay}s backoff (last: {type(last_err).__name__ if last_err else 'empty'})"
                    ),
                },
                agent=agent_name,
            )
            await asyncio.sleep(delay)
        parts: list[str] = []
        try:
            async for d in gateway.stream_completion(
                run_id=emitter.run_id,
                agent=agent_name,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
            ):
                parts.append(d)
            text = "".join(parts)
            if text.strip():
                return text
            # Empty 200 OK — treat as transient upstream glitch and retry.
            last_err = RuntimeError("upstream returned empty 200 OK response")
        except (
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.PoolTimeout,
        ) as e:
            last_err = e
        except httpx.HTTPStatusError as e:
            # 5xx → upstream had a transient problem; retry. 4xx is our bug
            # (auth, malformed request) and should fail loud, not retry.
            if e.response.status_code < 500:
                raise
            last_err = e
    raise AgentError(
        f"{agent_name} stream failed after {len(BACKOFFS)} attempts: {last_err}"
    ) from last_err


def _strip_json_fence(text: str) -> str:
    """Drop a leading ``` / ```json line and a trailing ``` if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


def _parse_json_lenient(text: str) -> Any:
    """Strict parse first, then fall back to raw_decode prefix recovery.

    Recovers the case where the model emitted a valid JSON object followed
    by free-form prose (a known gpt-5.5 failure mode on long generations).
    """
    import json as _stdlib_json  # local import keeps base.py header clean

    try:
        return orjson.loads(text)
    except Exception:  # orjson.JSONDecodeError or generic
        pass
    try:
        obj, end = _stdlib_json.JSONDecoder().raw_decode(text)
        if end < len(text.rstrip()):
            # Trailing junk was discarded — not an error, just a recovery.
            pass
        return obj
    except _stdlib_json.JSONDecodeError as e:
        raise AgentParseError(f"not valid JSON (raw_decode failed at pos {e.pos}): {e.msg}") from e


def _format_validation_error(err: ValidationError) -> str:
    """LLM-friendly rewrite of Pydantic validation errors.

    Mirrors claude-code-sourcemap's `formatZodValidationError` — produces
    short bullet lines like ``- Required field `references[0].doi` is missing``
    instead of the default URL-laden prose. The downstream
    `revise_with_critique` loop appends this verbatim to the next-turn
    user message, so concise + actionable wins big.
    """
    issues = err.errors()
    if not issues:
        return str(err)
    parts: list[str] = []
    for issue in issues:
        loc = ".".join(
            f"[{p}]" if isinstance(p, int) else str(p) for p in issue.get("loc", ())
        )
        if not loc:
            loc = "<root>"
        t = issue.get("type", "")
        msg = issue.get("msg", "")
        if t == "missing":
            parts.append(f"- Required field `{loc}` is missing")
        elif t == "extra_forbidden":
            parts.append(f"- Unexpected field `{loc}` (this schema forbids extras)")
        elif "type" in t or t in {"string_type", "int_type", "float_type", "bool_type", "list_type"}:
            got = issue.get("input")
            got_repr = (
                f"{type(got).__name__} {got!r}"
                if got is not None
                else "null"
            )
            parts.append(f"- Field `{loc}`: {msg}; got {got_repr}")
        else:
            parts.append(f"- `{loc}`: {msg}")
    return f"{len(issues)} validation issue(s):\n" + "\n".join(parts)


__all__ = ["AgentError", "AgentParseError", "BaseAgent"]
