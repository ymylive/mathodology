"""Searcher agent: derive queries from the Analyzer plan, hit arXiv + a web
source (Tavily or open-webSearch), synthesize findings.

Does NOT inherit from BaseAgent: there's a deterministic query-building step
plus tool calls sandwiched between stage.start and the single LLM synthesis
call. Lifecycle mirrors the other agents:

    stage.start → log(queries) → log(per-source hit counts)
                → log(total unique count) → agent.output → stage.done

Routing (see SearchConfig):
- arXiv is always hit first in parallel (we never skip it).
- `primary=tavily` → Tavily; if unique hits < `fallback_threshold` we also
  run open-webSearch as a top-up.
- `primary=open_websearch` → open-webSearch only (no fallback, there's
  nowhere to fall back TO).
- `primary=none` → skip the web leg entirely, arXiv only.

If every source returns nothing we still emit a minimal `SearchFindings`
with the queries recorded, so the downstream Writer can fall back cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import orjson
from mm_contracts import (
    AnalyzerOutput,
    Paper,
    ProblemInput,
    ReasoningEffort,
    SearchConfig,
    SearchFindings,
)
from pydantic import ValidationError

from agent_worker.agents.base import AgentError, AgentParseError
from agent_worker.config import get_settings
from agent_worker.events import EventEmitter
from agent_worker.gateway_client import GatewayClient
from agent_worker.prompts import load_prompt
from agent_worker.tools.arxiv import batch_search_arxiv
from agent_worker.tools.tavily import TavilyResult, batch_search_tavily
from agent_worker.tools.web_search_mcp import WebResult, batch_search_web

# Competition types for which a Chinese-language methodology query materially
# improves Baidu/CSDN/Juejin hit rate.
_ZH_COMPETITION = {"cumcm", "huashu", "other"}

# Exact-match tracking query keys (utm_* is handled by a prefix rule below);
# stripping them lets the web-dedupe collapse hits that differ only in noise.
_TRACKING_PARAMS = ("spm", "fromuid", "share")


def _has_cjk(text: str) -> bool:
    """True if the string contains at least one CJK Unified Ideograph."""
    return any("一" <= ch <= "鿿" for ch in text)


def _extract_zh_keywords(text: str, max_chars: int = 20) -> str:
    """Pull the first meaningful chunk of CJK text for a Baidu/CSDN query.

    We keep it deterministic and cheap — no jieba, no LLM — since this feeds
    a search engine which will do its own tokenization. Strips punctuation
    and ASCII, caps length.
    """
    buf: list[str] = []
    for ch in text:
        if "一" <= ch <= "鿿":
            buf.append(ch)
        elif ch.isspace() and buf and buf[-1] != " ":
            buf.append(" ")
        if len(buf) >= max_chars:
            break
    return "".join(buf).strip()


def _normalize_url(url: str) -> str:
    """Canonical form for dedupe: strip trailing slash + tracking params."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    path = parts.path.rstrip("/") or "/"
    # Filter out well-known tracking parameters; keep everything else.
    if parts.query:
        kept: list[str] = []
        for kv in parts.query.split("&"):
            if not kv:
                continue
            key = kv.split("=", 1)[0]
            # `utm_` is a prefix (utm_source, utm_campaign, ...); the others
            # are exact keys.
            if key.startswith("utm_"):
                continue
            if key in _TRACKING_PARAMS:
                continue
            kept.append(kv)
        query = "&".join(kept)
    else:
        query = ""
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, query, "")
    )

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
        model_override: str | None = None,
    ) -> None:
        self.gateway = gateway
        self.emitter = emitter
        self.prompt = load_prompt(self.AGENT_NAME, prompt_version)
        self._run_effort: ReasoningEffort = run_effort
        self._long_context: bool = long_context
        self._model_override: str | None = model_override

    async def run_for(
        self, problem: ProblemInput, analysis: AnalyzerOutput
    ) -> SearchFindings:
        """Build queries → arXiv + configured web source(s) → LLM synthesize."""
        t0 = time.monotonic()
        await self.emitter.emit(
            "stage.start", {"stage": self.AGENT_NAME}, agent=self.AGENT_NAME
        )

        # Phase 1: deterministic query derivation. No LLM call — sub_questions
        # and data_requirement names are already distilled signals.
        queries = self._build_queries(problem, analysis)
        await self.emitter.emit(
            "log",
            # Note: keep the "arXiv queries" prefix — legacy consumers grep
            # for this token. The same query list drives both arXiv and web.
            {"level": "info", "message": f"arXiv queries: {queries}"},
            agent=self.AGENT_NAME,
        )

        # Phase 2a: resolve routing. SearchConfig wins over env; env provides
        # the fallback default (engines list, kill-switch). If the user didn't
        # send a SearchConfig at all we synthesize one from env — this keeps
        # the worker runnable with the legacy payload shape.
        settings = get_settings()
        search_cfg = self._resolve_search_config(problem, settings)
        engines = tuple(search_cfg.engines)
        web_disabled = settings.open_websearch_disabled or not engines
        primary = search_cfg.primary

        # Phase 2b: arXiv always runs. It goes in parallel with the primary
        # web source (Tavily or open-webSearch). The fallback (if any) runs
        # sequentially AFTER the primary, because we only know whether to
        # invoke it once we've counted the primary's unique hits.
        async def _safe_arxiv() -> dict[str, list[Paper]]:
            try:
                return await batch_search_arxiv(
                    queries, max_per_query=5, concurrency=2
                )
            except Exception as e:  # noqa: BLE001
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"arXiv batch failed entirely: {e}",
                    },
                    agent=self.AGENT_NAME,
                )
                return {}

        async def _safe_tavily() -> dict[str, list[TavilyResult]]:
            if not settings.tavily_api_key:
                return {}
            try:
                return await batch_search_tavily(
                    queries,
                    settings.tavily_api_key,
                    depth=search_cfg.tavily_depth,
                    max_per_query=5,
                    concurrency=3,
                )
            except Exception as e:  # noqa: BLE001
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"tavily batch failed entirely: {e}",
                    },
                    agent=self.AGENT_NAME,
                )
                return {}

        async def _safe_web() -> dict[str, list[WebResult]]:
            if web_disabled:
                return {}
            try:
                return await batch_search_web(
                    queries,
                    engines=engines,
                    max_per_query=5,
                    concurrency=2,
                    command=settings.open_websearch_cmd,
                )
            except Exception as e:  # noqa: BLE001
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"web search batch failed entirely: {e}",
                    },
                    agent=self.AGENT_NAME,
                )
                return {}

        # Decide which "primary" source to run in parallel with arXiv. Empty
        # dicts mean "not selected"; that simplifies the merge below.
        tavily_results: dict[str, list[TavilyResult]] = {}
        web_results: dict[str, list[WebResult]] = {}
        effective_primary = primary  # may be auto-demoted below

        if primary == "tavily":
            if not settings.tavily_api_key:
                # Key missing → quietly demote to open_websearch for this run.
                effective_primary = "open_websearch"
                await self.emitter.emit(
                    "log",
                    {
                        "level": "info",
                        "message": (
                            "primary=tavily skipped: no TAVILY_API_KEY; "
                            "falling back to open_websearch"
                        ),
                    },
                    agent=self.AGENT_NAME,
                )
                arxiv_results, web_results = await asyncio.gather(
                    _safe_arxiv(), _safe_web()
                )
            else:
                arxiv_results, tavily_results = await asyncio.gather(
                    _safe_arxiv(), _safe_tavily()
                )
        elif primary == "open_websearch":
            arxiv_results, web_results = await asyncio.gather(
                _safe_arxiv(), _safe_web()
            )
        else:  # primary == "none"
            arxiv_results = await _safe_arxiv()

        # Per-source visibility — each source gets exactly one info log, in
        # fixed order (arXiv first, then whichever web source ran).
        await self.emitter.emit(
            "log",
            {
                "level": "info",
                "message": f"arXiv returned {sum(len(v) for v in arxiv_results.values())} papers",
            },
            agent=self.AGENT_NAME,
        )

        # Dedupe arXiv by arxiv_id (fallback url).
        arxiv_papers_flat = [p for ps in arxiv_results.values() for p in ps]
        seen_arxiv: set[str] = set()
        unique_arxiv: list[Paper] = []
        for p in arxiv_papers_flat:
            key = p.arxiv_id or p.url
            if key in seen_arxiv:
                continue
            seen_arxiv.add(key)
            unique_arxiv.append(p)

        # Unique-URL dedupe shared across Tavily + web hits. Web dedupe has
        # to span the fallback hop too — otherwise a site returned by both
        # Tavily AND open-webSearch would appear twice in `unique`.
        seen_web: set[str] = set()
        tavily_papers: list[Paper] = self._tavily_to_papers(
            tavily_results, seen_web
        )
        # Log Tavily's contribution if we actually ran it (even with 0 hits,
        # for visibility).
        if effective_primary == "tavily" and settings.tavily_api_key:
            await self.emitter.emit(
                "log",
                {
                    "level": "info",
                    "message": (
                        f"primary=tavily: {len(tavily_papers)} papers "
                        f"(depth={search_cfg.tavily_depth})"
                    ),
                },
                agent=self.AGENT_NAME,
            )

        web_papers: list[Paper] = self._web_to_papers(web_results, seen_web)
        # Log open-webSearch if it ran as primary (we log fallback separately).
        if effective_primary == "open_websearch" and not web_disabled:
            await self.emitter.emit(
                "log",
                {
                    "level": "info",
                    "message": (
                        f"primary=open_websearch: {len(web_papers)} papers "
                        f"across {len(engines)} engines"
                    ),
                },
                agent=self.AGENT_NAME,
            )

        # Phase 2c: fallback. Only Tavily-as-primary has a cheaper fallback
        # (open-webSearch). open_websearch / none have nothing to fall back
        # TO, so they skip this block.
        primary_paper_count = (
            len(tavily_papers) if effective_primary == "tavily" else 0
        )
        if (
            effective_primary == "tavily"
            and settings.tavily_api_key
            and primary_paper_count < search_cfg.fallback_threshold
            and not web_disabled
        ):
            await self.emitter.emit(
                "log",
                {
                    "level": "info",
                    "message": (
                        f"fallback triggered: primary had {primary_paper_count} "
                        f"< threshold {search_cfg.fallback_threshold}; "
                        f"running open_websearch"
                    ),
                },
                agent=self.AGENT_NAME,
            )
            web_results = await _safe_web()
            fallback_web_papers = self._web_to_papers(web_results, seen_web)
            web_papers.extend(fallback_web_papers)
            await self.emitter.emit(
                "log",
                {
                    "level": "info",
                    "message": (
                        f"fallback open_websearch: {len(fallback_web_papers)} "
                        f"papers across {len(engines)} engines"
                    ),
                },
                agent=self.AGENT_NAME,
            )

        unique: list[Paper] = unique_arxiv + tavily_papers + web_papers

        await self.emitter.emit(
            "log",
            {
                "level": "info",
                # Keep "unique papers" in the message — legacy log consumers
                # grep for it and the existing test suite depends on it.
                "message": (
                    f"retrieved {len(unique)} unique papers "
                    f"(arXiv={len(unique_arxiv)}, "
                    f"tavily={len(tavily_papers)}, "
                    f"web={len(web_papers)}) "
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

    @staticmethod
    def _resolve_search_config(
        problem: ProblemInput, settings: Any  # noqa: ANN401 — Settings, kept loose to avoid import cycle
    ) -> SearchConfig:
        """Return the effective SearchConfig for this run.

        User-supplied config (from `ProblemInput.search_config`) wins. Otherwise
        we synthesize one from env: `tavily` is the default when a key is
        present, else `open_websearch`. The engine list falls back to the
        comma-separated `OPEN_WEBSEARCH_ENGINES` env var.
        """
        if problem.search_config is not None:
            return problem.search_config
        engines_raw = [
            e.strip()
            for e in (settings.open_websearch_engines or "").split(",")
            if e.strip()
        ]
        # SearchConfig.engines is Literal-typed; silently drop unknown values
        # rather than refuse to construct. The typing is advisory for the UI,
        # not load-bearing for the Searcher.
        _valid = {"bing", "baidu", "duckduckgo", "csdn", "juejin",
                  "brave", "exa", "startpage"}
        engines = [e for e in engines_raw if e in _valid] or [
            "baidu", "csdn", "juejin", "duckduckgo"
        ]
        default_primary = "tavily" if settings.tavily_api_key else "open_websearch"
        return SearchConfig(
            primary=default_primary,  # type: ignore[arg-type]
            engines=engines,  # type: ignore[arg-type]
        )

    @staticmethod
    def _tavily_to_papers(
        tavily_results: dict[str, list[TavilyResult]],
        seen_urls: set[str],
    ) -> list[Paper]:
        """Flatten Tavily per-query results into deduped Paper records.

        `seen_urls` is an in/out set shared with the open-webSearch side so
        the same URL coming from both sources doesn't appear twice.
        """
        out: list[Paper] = []
        for hits in tavily_results.values():
            for t in hits:
                key = _normalize_url(t.url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                # Extract hostname for the relevance_reason prefix — the
                # Writer uses this to render references as
                # `引擎搜索得到 URL, 访问日期 ...`.
                try:
                    host = urlsplit(t.url).netloc.lower()
                except ValueError:
                    host = ""
                out.append(
                    Paper(
                        title=t.title,
                        authors=[],
                        abstract=(t.content or "")[:400],
                        url=t.url,
                        arxiv_id=None,
                        published=t.published_date,
                        relevance_reason=f"[tavily] {host or t.url}",
                    )
                )
        return out

    @staticmethod
    def _web_to_papers(
        web_results: dict[str, list[WebResult]],
        seen_urls: set[str],
    ) -> list[Paper]:
        """Flatten open-webSearch results into deduped Paper records.

        Mirrors `_tavily_to_papers`: `seen_urls` is shared across sources so
        cross-source duplicates collapse.
        """
        out: list[Paper] = []
        for hits in web_results.values():
            for w in hits:
                key = _normalize_url(w.url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                out.append(
                    Paper(
                        title=w.title,
                        authors=[],
                        abstract=(w.description or "")[:400],
                        url=w.url,
                        arxiv_id=None,
                        published=None,
                        relevance_reason=f"[{w.engine}] {w.source or w.url}",
                    )
                )
        return out

    def _build_queries(
        self, problem: ProblemInput, analysis: AnalyzerOutput
    ) -> list[str]:
        """Build queries from methodology-oriented terms (high hit rate) plus
        a couple of sub-questions (broader coverage). Falls back to the
        problem text if the Analyzer output is thin.

        For Chinese competitions (CUMCM / 华数杯 / other) we also append a
        single Chinese methodology query derived from the problem text —
        that's what makes Baidu/CSDN/Juejin actually return useful hits.
        """
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

        # Chinese-competition bonus query. Only emit when the problem text
        # actually contains CJK characters — otherwise we're wasting a slot.
        if (
            problem.competition_type in _ZH_COMPETITION
            and _has_cjk(problem.problem_text)
        ):
            keywords = _extract_zh_keywords(problem.problem_text)
            if keywords:
                qs.append(f"{keywords} 数学建模 最优化")

        return list(dict.fromkeys(q for q in qs if q))[:5]

    async def _synthesize(
        self,
        problem: ProblemInput,
        analysis: AnalyzerOutput,
        queries: list[str],
        papers: list[Paper],
    ) -> SearchFindings:
        """One LLM call to triage + summarize the retrieved papers."""
        model = self._model_override or self.prompt.model_preference[0]
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
