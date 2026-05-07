"""Crossref Works API client.

A thin async wrapper over `https://api.crossref.org/works`. Returns curated
`Paper` records for the SearcherAgent. Sibling to `arxiv.py` and
`openalex.py`: same shape, same best-effort contract — transient failures
yield `[]`, never exceptions.

Crossref is free, unauthenticated, and serves the polite pool when we
identify ourselves via `User-Agent: <ua>; mailto:<email>`. The mailto comes
from `MM_POLITE_MAILTO`; without it we still get served from the public
pool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from mm_contracts import Paper

_log = logging.getLogger(__name__)

CROSSREF_API_URL = "https://api.crossref.org/works"

_DEFAULT_USER_AGENT = "mathodology/1.0"


def _user_agent(mailto: str | None) -> str:
    if mailto:
        return f"{_DEFAULT_USER_AGENT} (mailto:{mailto})"
    return _DEFAULT_USER_AGENT


async def search_crossref(
    query: str,
    max_results: int = 5,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 — applied to the httpx client, not an asyncio primitive
    mailto: str | None = None,
) -> list[Paper]:
    """Query Crossref Works and return parsed Paper records.

    Empty / malformed responses degrade to `[]`. HTTP-level exceptions
    propagate when the caller passes its own client (test code); when we
    own the client we swallow them.
    """
    params: dict[str, Any] = {
        "query": query,
        "rows": max_results,
        # Restrict to article-shaped types — keeps proceedings, datasets, and
        # peer-review records out of the LLM's context.
        "filter": "type:journal-article,type:proceedings-article,type:posted-content",
        "select": "DOI,title,author,abstract,issued,URL",
    }
    headers = {"User-Agent": _user_agent(mailto)}

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        assert client is not None
        r = await client.get(CROSSREF_API_URL, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        return _parse_works(data)
    finally:
        if own_client and client is not None:
            await client.aclose()


def _parse_works(data: dict[str, Any]) -> list[Paper]:
    """Walk the Crossref JSON response and extract Paper records."""
    if not isinstance(data, dict):
        return []
    message = data.get("message")
    if not isinstance(message, dict):
        return []
    items = message.get("items")
    if not isinstance(items, list):
        return []

    papers: list[Paper] = []
    for item in items:
        try:
            paper = _item_to_paper(item)
        except Exception as e:  # noqa: BLE001
            _log.warning("Crossref item parse failed, skipping: %s", e)
            continue
        if paper is not None:
            papers.append(paper)
    return papers


def _item_to_paper(item: dict[str, Any]) -> Paper | None:
    if not isinstance(item, dict):
        return None

    titles = item.get("title")
    title = ""
    if isinstance(titles, list) and titles:
        first = titles[0]
        if isinstance(first, str):
            title = first.strip()
    elif isinstance(titles, str):
        title = titles.strip()
    if not title:
        return None

    raw_doi = item.get("DOI")
    doi: str | None = (
        raw_doi.strip() if isinstance(raw_doi, str) and raw_doi.strip() else None
    )

    if doi:
        url = f"https://doi.org/{doi}"
    else:
        raw_url = item.get("URL")
        if isinstance(raw_url, str) and raw_url.strip():
            url = raw_url.strip()
        else:
            return None

    authors: list[str] = []
    for a in item.get("author") or []:
        if not isinstance(a, dict):
            continue
        family = a.get("family") or ""
        given = a.get("given") or ""
        full = (f"{given} {family}".strip()) if (given or family) else ""
        if not full:
            full = a.get("name") or ""  # corporate / non-personal authors
        full = full.strip() if isinstance(full, str) else ""
        if full:
            authors.append(full)

    # Crossref abstracts are JATS XML fragments (<jats:p>…</jats:p>); strip
    # the wrapping tags. We deliberately do NOT do full XML parsing — the
    # abstract is fed verbatim to the LLM, which tolerates leftover whitespace.
    raw_abstract = item.get("abstract") or ""
    abstract = (
        _strip_jats_tags(raw_abstract).strip()
        if isinstance(raw_abstract, str)
        else ""
    )

    issued = item.get("issued")
    published = _format_issued_date(issued)

    return Paper(
        title=title,
        authors=authors[:20],
        abstract=abstract,
        url=url,
        doi=doi,
        published=published,
    )


def _strip_jats_tags(text: str) -> str:
    """Strip the most common JATS abstract tags. Best-effort, single pass."""
    out: list[str] = []
    in_tag = False
    for ch in text:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            out.append(" ")
            continue
        if not in_tag:
            out.append(ch)
    # Collapse repeated whitespace from inserted spaces.
    return " ".join("".join(out).split())


def _format_issued_date(issued: Any) -> str | None:
    """Convert Crossref's `issued.date-parts` to an ISO 8601 date string."""
    if not isinstance(issued, dict):
        return None
    parts_list = issued.get("date-parts")
    if not isinstance(parts_list, list) or not parts_list:
        return None
    first = parts_list[0]
    if not isinstance(first, list) or not first:
        return None
    try:
        nums = [int(x) for x in first if x is not None]
    except (TypeError, ValueError):
        return None
    if not nums:
        return None
    if len(nums) == 1:
        return f"{nums[0]:04d}"
    if len(nums) == 2:
        return f"{nums[0]:04d}-{nums[1]:02d}"
    return f"{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}"


async def batch_search_crossref(
    queries: list[str],
    max_per_query: int = 5,
    concurrency: int = 4,
    *,
    mailto: str | None = None,
) -> dict[str, list[Paper]]:
    """Run multiple Crossref queries in parallel with bounded concurrency.

    Never raises: a failed query contributes an empty list. Preserves input
    query order in the returned dict.
    """
    if not queries:
        return {}
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=10.0) as client:

        async def one(q: str) -> tuple[str, list[Paper]]:
            async with sem:
                try:
                    return q, await search_crossref(
                        q, max_per_query, client=client, mailto=mailto
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("Crossref query %r failed: %s", q, e)
                    return q, []

        pairs = await asyncio.gather(*[one(q) for q in queries])
    return dict(pairs)


__all__ = ["CROSSREF_API_URL", "batch_search_crossref", "search_crossref"]
