"""Searcher agent: derive queries from the Analyzer plan, hit arXiv, synthesize findings.

Does NOT inherit from BaseAgent: there's a deterministic query-building step
plus a tool (arXiv) call sandwiched between stage.start and the single LLM
synthesis call. Lifecycle mirrors the other agents:

    stage.start → log(queries) → log(unique paper count) → agent.output → stage.done

If arXiv returns nothing for every query (offline / rate-limited), we still
emit a minimal `SearchFindings` with the queries recorded and empty papers,
so the downstream Writer can fall back cleanly.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import orjson
from mm_contracts import (
    AnalyzerOutput,
    Paper,
    ProblemInput,
    ReasoningEffort,
    SearchFindings,
)
from pydantic import ValidationError

from agent_worker.agents.base import AgentError, AgentParseError
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.prompts import load_prompt
from agent_worker.tools.arxiv import batch_search_arxiv

_log = logging.getLogger(__name__)


class SearcherAgent:
    """Third stage of the pipeline (M10): arXiv retrieval + LLM synthesis."""

    AGENT_NAME = "searcher"
    OUTPUT_MODEL = SearchFindings

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
        self._run_effort: ReasoningEffort = run_effort
        self._long_context: bool = long_context

    async def run_for(
        self, problem: ProblemInput, analysis: AnalyzerOutput
    ) -> SearchFindings:
        """Build queries → hit arXiv → LLM synthesize → SearchFindings."""
        t0 = time.monotonic()
        await self.emitter.emit(
            "stage.start", {"stage": self.AGENT_NAME}, agent=self.AGENT_NAME
        )

        # Phase 1: deterministic query derivation. No LLM call — sub_questions
        # and data_requirement names are already distilled signals.
        queries = self._build_queries(problem, analysis)
        await self.emitter.emit(
            "log",
            {"level": "info", "message": f"arXiv queries: {queries}"},
            agent=self.AGENT_NAME,
        )

        # Phase 2: arXiv fetch (parallel, bounded). Never raises.
        try:
            results = await batch_search_arxiv(
                queries, max_per_query=5, concurrency=2
            )
        except Exception as e:  # noqa: BLE001 — belt-and-suspenders; batch already catches
            await self.emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": f"arXiv batch failed entirely: {e}",
                },
                agent=self.AGENT_NAME,
            )
            results = {}

        all_papers = [p for papers in results.values() for p in papers]
        seen: set[str] = set()
        unique: list[Paper] = []
        for p in all_papers:
            key = p.arxiv_id or p.url
            if key in seen:
                continue
            seen.add(key)
            unique.append(p)
        await self.emitter.emit(
            "log",
            {
                "level": "info",
                "message": (
                    f"arXiv returned {len(unique)} unique papers "
                    f"across {len(queries)} queries"
                ),
            },
            agent=self.AGENT_NAME,
        )

        # Phase 3: LLM synthesis. If arXiv returned nothing across the board,
        # skip the LLM and emit a minimal SearchFindings — no point asking the
        # model to curate an empty list.
        if not unique:
            await self.emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": "arXiv returned 0 papers; emitting empty SearchFindings",
                },
                agent=self.AGENT_NAME,
            )
            findings = SearchFindings(queries=queries)
        else:
            findings = await self._synthesize(problem, analysis, queries, unique)

        duration_ms = int((time.monotonic() - t0) * 1000)
        await self.emitter.emit(
            "agent.output",
            {
                "schema_name": "SearchFindings",
                "output": findings.model_dump(mode="json"),
                "duration_ms": duration_ms,
            },
            agent=self.AGENT_NAME,
        )
        await self.emitter.emit(
            "stage.done",
            {"stage": self.AGENT_NAME, "duration_ms": duration_ms},
            agent=self.AGENT_NAME,
        )
        return findings

    # ------------------------------------------------------------------ helpers

    def _build_queries(
        self, problem: ProblemInput, analysis: AnalyzerOutput
    ) -> list[str]:
        """Build arXiv queries from methodology-oriented terms (high hit rate)
        plus a couple of sub-questions (broader coverage). Falls back to the
        problem text if the Analyzer output is thin."""
        qs: list[str] = []
        # Methodological terms first — these match arXiv keywords reliably.
        for appr in (analysis.proposed_approaches or [])[:2]:
            if appr.name:
                qs.append(appr.name.strip())
            for m in (appr.methods or [])[:2]:
                if m and not m.startswith(("numpy.", "scipy.", "sklearn.")):
                    qs.append(m.strip())
        # Broader signals from sub-questions (up to 2, not the whole phrase —
        # strip trailing ? to avoid hurting BM25-style tokenizers).
        for sq in (analysis.sub_questions or [])[:2]:
            if sq:
                qs.append(sq.strip().rstrip("?"))
        # Data-requirement names tend to be specific filenames (poor arXiv
        # signal) — skip unless nothing else is available.
        if not qs:
            for dr in (analysis.data_requirements or [])[:2]:
                if dr.name:
                    qs.append(dr.name.strip())
        if not qs:
            qs.append(problem.problem_text[:120])
        return list(dict.fromkeys(q for q in qs if q))[:5]

    async def _synthesize(
        self,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        queries: list[str],
        papers: list[Paper],
    ) -> SearchFindings:
        """One LLM call to triage + summarize the retrieved papers."""
        model = self.prompt.model_preference[0]
        papers_payload = [
            {
                "title": p.title,
                "authors": p.authors[:5],
                "abstract": (p.abstract or "")[:400],
                "url": p.url,
                "arxiv_id": p.arxiv_id,
                "published": p.published,
            }
            for p in papers
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {
                "role": "user",
                "content": self.prompt.render_user(
                    problem_text=problem.problem_text,
                    competition_type=problem.competition_type,
                    analysis_json=json.dumps(
                        analysis.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    papers_json=json.dumps(
                        papers_payload, ensure_ascii=False, indent=2
                    ),
                    queries_json=json.dumps(queries, ensure_ascii=False),
                ),
            },
        ]
        return await self._ask_llm(model, messages)

    async def _ask_llm(
        self, model: str, messages: list[dict[str, Any]]
    ) -> SearchFindings:
        """Stream completion; parse with one retry; emit error + raise AgentError on final failure."""
        attempts = 0
        last_err: Exception | None = None
        local_messages = list(messages)
        while attempts < 2:
            attempts += 1
            text = await self._stream_and_collect(model, local_messages)
            try:
                return self._parse_findings(text)
            except AgentParseError as e:
                last_err = e
                local_messages = [
                    *local_messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response could not be parsed as "
                            f"SearchFindings JSON. Error: {e}. Return ONLY a "
                            "valid JSON object with keys queries, papers, "
                            "key_findings, datasets_mentioned."
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
    def _parse_findings(text: str) -> SearchFindings:
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
            return SearchFindings.model_validate(obj)
        except ValidationError as e:
            raise AgentParseError(str(e)) from e


__all__ = ["SearcherAgent"]
