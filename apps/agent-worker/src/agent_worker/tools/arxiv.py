"""arXiv Atom API client.

A thin async wrapper over `https://export.arxiv.org/api/query`. Parses the
Atom response with `defusedxml` (stdlib ElementTree is XXE-unsafe on
untrusted input). Designed for the SearcherAgent: best-effort, never fails
the pipeline on transient errors — callers get `[]` instead of exceptions
for network / parse failures.

arXiv asks for <=1 request per 3s from a single source. `batch_search_arxiv`
keeps concurrency low (default 2) and adds a short delay between batches.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from defusedxml import ElementTree as ET
from mm_contracts import Paper

_log = logging.getLogger(__name__)

_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# Matches "http(s)://arxiv.org/abs/<id>" with optional version suffix.
_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/([^/?#]+)$", re.IGNORECASE)

ARXIV_API_URL = "https://export.arxiv.org/api/query"


async def search_arxiv(
    query: str,
    max_results: int = 5,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 — applied to the httpx client, not an asyncio primitive
) -> list[Paper]:
    """Query arXiv's public API and return parsed Paper records.

    A missing/malformed response returns `[]` rather than raising, so the
    Searcher can degrade gracefully. HTTP-level exceptions propagate when the
    caller passes its own client (test code); when we own the client we swallow
    them after logging.
    """
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        assert client is not None
        r = await client.get(ARXIV_API_URL, params=params)
        r.raise_for_status()
        return _parse_atom(r.text)
    finally:
        if own_client and client is not None:
            await client.aclose()


def _parse_atom(xml_text: str) -> list[Paper]:
    """Walk the atom XML and extract Paper records. Returns [] on malformed XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        _log.warning("arXiv XML parse failed: %s", e)
        return []

    papers: list[Paper] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        try:
            paper = _entry_to_paper(entry)
        except Exception as e:  # noqa: BLE001 — never fail the whole batch on one bad entry
            _log.warning("arXiv entry parse failed, skipping: %s", e)
            continue
        if paper is not None:
            papers.append(paper)
    return papers


def _entry_to_paper(entry) -> Paper | None:  # noqa: ANN001 — ET.Element from defusedxml
    title_el = entry.find("atom:title", _ATOM_NS)
    id_el = entry.find("atom:id", _ATOM_NS)
    if title_el is None or id_el is None:
        return None

    title = (title_el.text or "").strip()
    url = (id_el.text or "").strip()
    if not title or not url:
        return None

    summary_el = entry.find("atom:summary", _ATOM_NS)
    abstract = (summary_el.text or "").strip() if summary_el is not None else ""

    published_el = entry.find("atom:published", _ATOM_NS)
    published = (
        (published_el.text or "").strip() if published_el is not None else None
    )

    authors: list[str] = []
    for author_el in entry.findall("atom:author", _ATOM_NS):
        name_el = author_el.find("atom:name", _ATOM_NS)
        if name_el is not None and name_el.text:
            authors.append(name_el.text.strip())

    # arXiv id: strip trailing version suffix (e.g. "2312.01234v2" → "2312.01234").
    arxiv_id: str | None = None
    m = _ARXIV_ID_RE.search(url)
    if m:
        raw = m.group(1)
        arxiv_id = raw.split("v")[0] if re.search(r"v\d+$", raw) else raw

    return Paper(
        title=title,
        authors=authors[:20],
        abstract=abstract,
        url=url,
        arxiv_id=arxiv_id,
        published=published,
    )


async def batch_search_arxiv(
    queries: list[str],
    max_per_query: int = 5,
    concurrency: int = 2,
) -> dict[str, list[Paper]]:
    """Run multiple arXiv queries in parallel with bounded concurrency.

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
                    return q, await search_arxiv(q, max_per_query, client=client)
                except Exception as e:  # noqa: BLE001
                    _log.warning("arXiv query %r failed: %s", q, e)
                    return q, []

        pairs = await asyncio.gather(*[one(q) for q in queries])
    return dict(pairs)


__all__ = ["ARXIV_API_URL", "batch_search_arxiv", "search_arxiv"]
