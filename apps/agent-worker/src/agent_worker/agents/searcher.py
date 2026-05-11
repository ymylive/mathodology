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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import orjson
from mm_contracts import (
    AnalyzerOutput,
    CritiqueReport,
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
from agent_worker.tools.crossref import batch_search_crossref
from agent_worker.tools.openalex import batch_search_openalex
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


# --- PDF enrichment / compaction tuning -------------------------------------
COMPACT_THRESHOLD_CHARS = 24_000
COMPACT_TARGET_CHARS = 24_000
COMPACT_MIN_OUTPUT_CHARS = 1_000

_COMPACT_SYSTEM_PROMPT = """You compress an academic paper into a high-density summary for downstream LLM citation. Preserve verbatim:
- Mathematical formulas (LaTeX or plain)
- Parameter definitions and units
- Methodology steps in order
- Experimental setup (sample size, data source, conditions)
- Numerical results with confidence intervals or error bars
- Key claims that Writer might cite

Drop entirely:
- Boilerplate (acknowledgments, ethics, conflicts of interest)
- Related-work prose (the Writer has SearchFindings.papers for that)
- Narrative filler ("In this paper we propose...")
- Reference list
- Repeated information

Preserve the source language. If the input is Chinese, output Chinese.
If English, output English. Do not translate.

Output: dense markdown, ≤24000 characters. Keep section headings
(## Methods, ## Results, etc.) so Writer can grep for them.
Respond with the compacted markdown only."""

# Sentinel for _stream_and_collect's default. Passing response_format=None
# explicitly disables the JSON-object constraint (used by _compact_one); the
# default preserves backward compat for _ask_llm / _refine_queries.
_DEFAULT_JSON_RESPONSE_FORMAT: dict[str, Any] = {"type": "json_object"}


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
        raw_queries = self._build_queries(problem, analysis)

        # Phase 1b: refine raw queries into bibliographic search strings via
        # one small LLM call. The Analyzer surfaces sub-questions as modeling
        # steps ("使用 M/M/c 稳态公式计算 P0、Lq、Wq...") which are awful as
        # search inputs — every scholarly DB returned 0 actually-relevant
        # papers on those, and the synthesize step then dropped them all.
        # The rewriter produces 3 English + (1 Chinese for CJK problems)
        # queries focused on domain noun phrases. Falls back to the raw set
        # whenever the call or the JSON parse fails.
        queries = await self._refine_queries(problem, raw_queries)
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

        # Phase 2b: scholarly sources (arXiv + OpenAlex + Crossref) always
        # run in parallel with the primary web source. Each is independently
        # best-effort: a single source 429ing or 5xxing degrades to an empty
        # dict, so the Searcher never goes silent unless every source fails.
        # The web fallback (if any) runs sequentially AFTER the primary —
        # we only know whether to invoke it once we've counted primary hits.
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

        async def _safe_openalex() -> dict[str, list[Paper]]:
            if settings.openalex_disabled:
                return {}
            try:
                return await batch_search_openalex(
                    queries,
                    max_per_query=5,
                    concurrency=4,
                    mailto=settings.polite_mailto or None,
                )
            except Exception as e:  # noqa: BLE001
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"OpenAlex batch failed entirely: {e}",
                    },
                    agent=self.AGENT_NAME,
                )
                return {}

        async def _safe_crossref() -> dict[str, list[Paper]]:
            if settings.crossref_disabled:
                return {}
            try:
                return await batch_search_crossref(
                    queries,
                    max_per_query=5,
                    concurrency=4,
                    mailto=settings.polite_mailto or None,
                )
            except Exception as e:  # noqa: BLE001
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"Crossref batch failed entirely: {e}",
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

        # Decide which "primary" web source to run in parallel with the
        # scholarly trio (arXiv + OpenAlex + Crossref). Empty dicts mean "not
        # selected"; that simplifies the merge below.
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
                (
                    arxiv_results,
                    openalex_results,
                    crossref_results,
                    web_results,
                ) = await asyncio.gather(
                    _safe_arxiv(),
                    _safe_openalex(),
                    _safe_crossref(),
                    _safe_web(),
                )
            else:
                (
                    arxiv_results,
                    openalex_results,
                    crossref_results,
                    tavily_results,
                ) = await asyncio.gather(
                    _safe_arxiv(),
                    _safe_openalex(),
                    _safe_crossref(),
                    _safe_tavily(),
                )
        elif primary == "open_websearch":
            (
                arxiv_results,
                openalex_results,
                crossref_results,
                web_results,
            ) = await asyncio.gather(
                _safe_arxiv(),
                _safe_openalex(),
                _safe_crossref(),
                _safe_web(),
            )
        else:  # primary == "none"
            arxiv_results, openalex_results, crossref_results = await asyncio.gather(
                _safe_arxiv(),
                _safe_openalex(),
                _safe_crossref(),
            )

        # Per-source visibility — each scholarly source gets one info log
        # (arXiv first, then OpenAlex, then Crossref); the web source logs
        # below are unchanged.
        await self.emitter.emit(
            "log",
            {
                "level": "info",
                "message": f"arXiv returned {sum(len(v) for v in arxiv_results.values())} papers",
            },
            agent=self.AGENT_NAME,
        )
        await self.emitter.emit(
            "log",
            {
                "level": "info",
                "message": (
                    f"OpenAlex returned {sum(len(v) for v in openalex_results.values())} papers"
                ),
            },
            agent=self.AGENT_NAME,
        )
        await self.emitter.emit(
            "log",
            {
                "level": "info",
                "message": (
                    f"Crossref returned {sum(len(v) for v in crossref_results.values())} papers"
                ),
            },
            agent=self.AGENT_NAME,
        )

        # Dedupe scholarly papers across the three sources. Identity keys, in
        # priority order: arxiv_id, doi, url. Iteration order arXiv → OpenAlex
        # → Crossref so an arXiv preprint that also appears as a published
        # version in OpenAlex/Crossref is kept under its arXiv URL.
        seen_scholarly: set[str] = set()
        unique_arxiv: list[Paper] = []
        unique_openalex: list[Paper] = []
        unique_crossref: list[Paper] = []

        def _scholar_keys(paper: Paper) -> list[str]:
            keys: list[str] = []
            if paper.arxiv_id:
                keys.append(f"arxiv:{paper.arxiv_id}")
            if paper.doi:
                keys.append(f"doi:{paper.doi.lower()}")
            keys.append(f"url:{paper.url}")
            return keys

        def _take_unique(
            papers_dict: dict[str, list[Paper]], dest: list[Paper]
        ) -> None:
            for ps in papers_dict.values():
                for p in ps:
                    keys = _scholar_keys(p)
                    if any(k in seen_scholarly for k in keys):
                        continue
                    seen_scholarly.update(keys)
                    dest.append(p)

        _take_unique(arxiv_results, unique_arxiv)
        _take_unique(openalex_results, unique_openalex)
        _take_unique(crossref_results, unique_crossref)
        unique_scholarly: list[Paper] = (
            unique_arxiv + unique_openalex + unique_crossref
        )

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

        unique: list[Paper] = unique_scholarly + tavily_papers + web_papers

        await self.emitter.emit(
            "log",
            {
                "level": "info",
                # Keep "unique papers" in the message — legacy log consumers
                # grep for it and the existing test suite depends on it.
                "message": (
                    f"retrieved {len(unique)} unique papers "
                    f"(arXiv={len(unique_arxiv)}, "
                    f"openalex={len(unique_openalex)}, "
                    f"crossref={len(unique_crossref)}, "
                    f"tavily={len(tavily_papers)}, "
                    f"web={len(web_papers)}) "
                    f"across {len(queries)} queries"
                ),
            },
            agent=self.AGENT_NAME,
        )

        # Phase 3: LLM synthesis. If every source came back empty (arXiv +
        # OpenAlex + Crossref + tavily/web), skip the LLM and emit a minimal
        # SearchFindings — no point asking the model to curate an empty list.
        if not unique:
            await self.emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": (
                        "all search sources returned 0 papers; "
                        "emitting empty SearchFindings"
                    ),
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

    async def _refine_queries(
        self, problem: ProblemInput, raw_queries: list[str]
    ) -> list[str]:
        """LLM-rewrite raw queries into bibliographic search strings.

        Returns up to 6 queries: ~3 English ones (consumed by arXiv /
        OpenAlex / Crossref) plus ~1 Chinese one when the problem is in
        CJK (consumed by Baidu / CSDN / Juejin via open-webSearch). Strict
        fallback: any LLM failure or parse error returns ``raw_queries``
        unchanged so search never fails on a flaky rewrite.
        """
        if not raw_queries:
            return raw_queries
        is_cjk = _has_cjk(problem.problem_text)
        zh_count = 1 if is_cjk else 0
        en_count = 4 - zh_count

        sys_text = (
            "You rewrite math-modeling problem fragments into well-formed "
            "bibliographic search queries for scholarly databases (arXiv, "
            "OpenAlex, Crossref) and Chinese web search engines.\n"
            "Strict rules:\n"
            "- Drop pure formula notation, parameter definitions, and "
            "step-by-step descriptions. They never retrieve relevant papers.\n"
            "- Keep domain noun phrases and canonical method names "
            "(e.g. 'M/M/c queue', 'Erlang C', '排队论 银行 网点').\n"
            "- Each query is 3 to 10 tokens, focused on the academic topic.\n"
            "- Output JSON only, no commentary."
        )
        user_text = (
            f"Problem (truncated to 300 chars):\n{problem.problem_text[:300]}\n\n"
            "Raw analyzer-driven queries (likely poorly shaped):\n"
            + "\n".join(f"- {q}" for q in raw_queries)
            + f"\n\nProduce exactly {en_count} English bibliographic queries "
            f"and {zh_count} Chinese ones, in priority order.\n"
            'Respond with ONLY this JSON shape: {"queries": ["...", "..."]}'
        )
        model = self._model_override or self.prompt.model_preference[0]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": user_text},
        ]
        try:
            text = await self._stream_and_collect(model, messages)
            data = orjson.loads(text)
            refined = data.get("queries") if isinstance(data, dict) else None
            if not isinstance(refined, list):
                raise ValueError("missing 'queries' array in LLM response")
            cleaned = [
                q.strip() for q in refined if isinstance(q, str) and q.strip()
            ]
            if not cleaned:
                raise ValueError("LLM returned no usable queries")
            # Dedupe, cap. We deliberately do NOT mix raw + refined: if the
            # rewriter succeeded, its queries are uniformly better; if it
            # failed, we already fell into the except branch.
            return list(dict.fromkeys(cleaned))[:6]
        except Exception as e:  # noqa: BLE001 — never fail Searcher on rewrite
            await self.emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": (
                        f"query refinement failed: {e}; "
                        f"using raw queries verbatim"
                    ),
                },
                agent=self.AGENT_NAME,
            )
            return raw_queries

    async def _compact_oversized_papers(
        self, runs_papers_dir: Path, paths: list[str]
    ) -> list[str]:
        """Compact any persisted paper text exceeding COMPACT_THRESHOLD_CHARS.

        Overwrites in place. Compaction failure leaves the raw file untouched —
        the Writer side has its own char-budget soft truncation as a safety
        net. Returns the same paths argument unchanged (compaction never drops
        a paper; it either improves the file or leaves it alone).
        """
        run_dir = runs_papers_dir.parent
        for rel_path in paths:
            abs_path = run_dir / rel_path
            try:
                raw = abs_path.read_text("utf-8")
            except OSError as e:
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"compact: could not read {rel_path}: {e}",
                    },
                    agent=self.AGENT_NAME,
                )
                continue
            if len(raw) <= COMPACT_THRESHOLD_CHARS:
                continue
            compacted = await self._compact_one(raw)
            if not compacted or len(compacted) < COMPACT_MIN_OUTPUT_CHARS:
                await self.emitter.emit(
                    "log",
                    {
                        "level": "warning",
                        "message": (
                            f"compact: keeping raw for {rel_path} "
                            f"(compaction unusable)"
                        ),
                    },
                    agent=self.AGENT_NAME,
                )
                continue
            abs_path.write_text(compacted, encoding="utf-8")
            await self.emitter.emit(
                "log",
                {
                    "level": "info",
                    "message": (
                        f"compact: {rel_path} {len(raw)} → {len(compacted)} chars"
                    ),
                },
                agent=self.AGENT_NAME,
            )
        return paths

    async def _compact_one(self, raw_text: str) -> str | None:
        """Run one LLM call to compress raw paper text. None on failure."""
        model = self._model_override or self.prompt.model_preference[0]
        user_text = (
            "Compact this paper text into ≤24000 characters of dense markdown, "
            "preserving the source language verbatim.\n\nRaw paper text:\n\n"
            + raw_text
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        try:
            text = await self._stream_and_collect(
                model, messages, response_format=None
            )
        except Exception as e:  # noqa: BLE001
            await self.emitter.emit(
                "log",
                {
                    "level": "warning",
                    "message": f"compact: LLM call failed: {e}",
                },
                agent=self.AGENT_NAME,
            )
            return None
        text = text.strip() if isinstance(text, str) else ""
        return text or None

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

    async def revise_with_critique(
        self,
        *,
        original_output: SearchFindings,
        critique: CritiqueReport,
        context: dict[str, Any],
    ) -> SearchFindings:
        """Revise synthesized findings without re-running external search tools."""
        model = self._model_override or self.prompt.model_preference[0]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.system["text"]},
            {
                "role": "user",
                "content": (
                    "Revise the previous SearchFindings JSON using the Critic "
                    "feedback below. Do not invent new papers, URLs, datasets, "
                    "or claims. Preserve valid retrieved sources and only adjust "
                    "queries, key_findings, datasets_mentioned, and source "
                    "selection when supported by the original output.\n\n"
                    "Context JSON:\n"
                    f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
                    "Original SearchFindings JSON:\n"
                    f"{original_output.model_dump_json(indent=2)}\n\n"
                    "Critique JSON:\n"
                    f"{critique.model_dump_json(indent=2)}"
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
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = _DEFAULT_JSON_RESPONSE_FORMAT,
    ) -> str:
        """Stream completion; concat deltas; return raw text.

        ``response_format`` defaults to ``{"type": "json_object"}`` to preserve
        the existing JSON-only behavior used by ``_ask_llm`` /
        ``_refine_queries``. Pass ``response_format=None`` for free-form text
        responses (e.g. the compaction LLM call, which returns markdown).
        """
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
            response_format=response_format,
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
