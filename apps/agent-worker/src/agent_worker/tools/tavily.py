"""Tavily search client — primary web-search source for the Searcher agent.

A thin async wrapper over `POST https://api.tavily.com/search`. Modeled on
`tools/arxiv.py::batch_search_arxiv` and `tools/web_search_mcp.py::batch_search_web`:
- bounded concurrency via asyncio.Semaphore (Tavily's free tier is rate-limited;
  concurrency=3 is conservative headroom)
- per-query timeout drops only that query (never cascades)
- auth / rate-limit failures log a warning and return empty results

API shape (from https://docs.tavily.com):
    POST https://api.tavily.com/search
    Content-Type: application/json
    {
      "api_key": "...",
      "query": "...",
      "search_depth": "basic" | "advanced",
      "max_results": 5,
      "include_answer": false,
      "include_raw_content": false
    }
    →
    {
      "query": "...",
      "results": [
        {"title": "...", "url": "...", "content": "...",
         "score": 0.87, "published_date": "2024-01-01" | null},
        ...
      ],
      ...
    }
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

_log = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class TavilyResult:
    """One hit from Tavily (normalized to match WebResult conventions)."""

    title: str
    url: str
    content: str  # Tavily's snippet — used downstream as pseudo-abstract
    score: float  # Tavily's own relevance score in [0, 1]
    published_date: str | None = None
    query: str = ""  # original user query that produced this hit


def _parse_results(payload: Any, query: str) -> list[TavilyResult]:  # noqa: ANN401
    """Pull `results` out of a Tavily response body. Returns [] on any shape issue."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    out: list[TavilyResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title or not url:
            continue
        content = item.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        score_raw = item.get("score", 0.0)
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0
        published = item.get("published_date")
        if published is not None and not isinstance(published, str):
            published = str(published)
        out.append(
            TavilyResult(
                title=title,
                url=url,
                content=content.strip(),
                score=score,
                published_date=published or None,
                query=query,
            )
        )
    return out


async def search_tavily(
    query: str,
    api_key: str,
    *,
    depth: Literal["basic", "advanced"] = "basic",
    max_results: int = 5,
    client: httpx.AsyncClient | None = None,
    timeout: float = 30.0,  # noqa: ASYNC109 — httpx client timeout, not an asyncio primitive
) -> list[TavilyResult]:
    """Run a single Tavily query. Returns [] on any non-2xx response.

    Designed as a degrade-gracefully leaf for the Searcher: callers pass their
    own `AsyncClient` (pooled across a batch) so we don't thrash connections.
    """
    body = {
        "api_key": api_key,
        "query": query,
        "search_depth": depth,
        "max_results": max_results,
        # Tavily can synthesize an answer / include raw HTML; neither is useful
        # for our downstream LLM synthesis and both inflate the response.
        "include_answer": False,
        "include_raw_content": False,
    }
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        assert client is not None
        r = await client.post(TAVILY_API_URL, json=body)
        if r.status_code in (401, 403):
            _log.warning(
                "tavily auth failed (%s): check TAVILY_API_KEY", r.status_code
            )
            return []
        if r.status_code == 429:
            _log.warning("tavily rate-limited (429) on query %r", query)
            return []
        if r.status_code >= 400:
            _log.warning(
                "tavily HTTP %s on query %r: %s",
                r.status_code,
                query,
                r.text[:200],
            )
            return []
        try:
            data = r.json()
        except ValueError as e:
            _log.warning("tavily returned non-JSON for %r: %s", query, e)
            return []
        return _parse_results(data, query)
    finally:
        if own_client and client is not None:
            await client.aclose()


async def batch_search_tavily(
    queries: Sequence[str],
    api_key: str,
    *,
    depth: Literal["basic", "advanced"] = "basic",
    max_per_query: int = 5,
    concurrency: int = 3,
    timeout_s: float = 30.0,
) -> dict[str, list[TavilyResult]]:
    """Run N queries through Tavily in parallel, bounded by `concurrency`.

    Returns a dict mapping every input query → list of results (empty list on
    per-query failure). Never raises:
      - missing / invalid api_key → all queries get []
      - 401 / 403 / 429 → that query gets [], no cascade
      - per-query timeout → that query gets [], others unaffected

    Preserves input query order in the returned dict.
    """
    if not queries:
        return {}
    if not api_key:
        # Surface once at the batch level — per-query logging would be noisy.
        _log.warning("tavily skipped: no api_key provided")
        return {q: [] for q in queries}

    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, list[TavilyResult]] = {q: [] for q in queries}

    async with httpx.AsyncClient(timeout=timeout_s) as client:

        async def _one(q: str) -> None:
            async with sem:
                try:
                    res = await asyncio.wait_for(
                        search_tavily(
                            q,
                            api_key,
                            depth=depth,
                            max_results=max_per_query,
                            client=client,
                        ),
                        timeout=timeout_s,
                    )
                except TimeoutError:
                    _log.warning(
                        "tavily timed out after %.1fs: %r", timeout_s, q
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    _log.warning("tavily errored on %r: %s", q, e)
                    return
                results[q] = res

        await asyncio.gather(*[_one(q) for q in queries])

    return results


__all__ = [
    "TAVILY_API_URL",
    "TavilyResult",
    "batch_search_tavily",
    "search_tavily",
]
