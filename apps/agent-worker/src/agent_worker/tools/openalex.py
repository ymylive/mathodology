"""OpenAlex Works API client.

A thin async wrapper over `https://api.openalex.org/works`. Returns curated
`Paper` records for the SearcherAgent. Designed as an arXiv companion: same
shape, same best-effort contract — transient failures yield `[]`, never
exceptions.

OpenAlex is free and unauthenticated for ≤100k req/day. Including
`mailto=<email>` in the query routes us to the polite pool (faster, more
forgiving rate limits). The mailto is read from `MM_POLITE_MAILTO` and
omitted when unset.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from mm_contracts import Paper

_log = logging.getLogger(__name__)

OPENALEX_API_URL = "https://api.openalex.org/works"


async def search_openalex(
    query: str,
    max_results: int = 5,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 — applied to the httpx client, not an asyncio primitive
    mailto: str | None = None,
) -> list[Paper]:
    """Query OpenAlex Works and return parsed Paper records.

    Empty / malformed responses degrade to `[]` so the Searcher keeps
    running. HTTP-level exceptions propagate when the caller passes its
    own client (test code); when we own the client we swallow them.
    """
    params: dict[str, Any] = {
        "search": query,
        "per-page": max_results,
        # Only ask for the fields we actually consume; cuts payload size
        # roughly 10x compared to default.
        "select": "id,doi,title,publication_date,authorships,abstract_inverted_index",
    }
    if mailto:
        params["mailto"] = mailto

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        assert client is not None
        r = await client.get(OPENALEX_API_URL, params=params)
        r.raise_for_status()
        data = r.json()
        return _parse_works(data)
    finally:
        if own_client and client is not None:
            await client.aclose()


def _parse_works(data: dict[str, Any]) -> list[Paper]:
    """Walk the OpenAlex JSON response and extract Paper records."""
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []

    papers: list[Paper] = []
    for item in results:
        try:
            paper = _work_to_paper(item)
        except Exception as e:  # noqa: BLE001 — never fail the whole batch on one bad entry
            _log.warning("OpenAlex work parse failed, skipping: %s", e)
            continue
        if paper is not None:
            papers.append(paper)
    return papers


def _work_to_paper(item: dict[str, Any]) -> Paper | None:
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or "").strip()
    if not title:
        return None

    raw_doi = item.get("doi")
    doi: str | None = None
    if isinstance(raw_doi, str) and raw_doi.strip():
        # OpenAlex returns DOIs already prefixed with `https://doi.org/`;
        # store the bare DOI plus a clean URL so cross-source dedupe works.
        candidate = raw_doi.strip()
        if candidate.lower().startswith("https://doi.org/"):
            doi = candidate[len("https://doi.org/") :]
        elif candidate.lower().startswith("http://doi.org/"):
            doi = candidate[len("http://doi.org/") :]
        else:
            doi = candidate

    if doi:
        url = f"https://doi.org/{doi}"
    else:
        # Fall back to the OpenAlex work id URL. Always present.
        oa_id = item.get("id")
        if not isinstance(oa_id, str) or not oa_id.strip():
            return None
        url = oa_id.strip()

    published = item.get("publication_date")
    if not isinstance(published, str) or not published.strip():
        published = None

    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if isinstance(author, dict):
            name = author.get("display_name")
            if isinstance(name, str) and name.strip():
                authors.append(name.strip())

    abstract = _reconstruct_inverted_abstract(item.get("abstract_inverted_index"))

    return Paper(
        title=title,
        authors=authors[:20],
        abstract=abstract,
        url=url,
        doi=doi,
        published=published,
    )


def _reconstruct_inverted_abstract(idx: Any) -> str:
    """OpenAlex stores abstracts as `{word: [positions]}`; rebuild the prose.

    Returns "" if the index is missing or malformed. The output is a single
    space-joined string; OpenAlex does not preserve original punctuation
    locations, so this is best-effort prose for the LLM to read.
    """
    if not isinstance(idx, dict) or not idx:
        return ""
    pairs: list[tuple[int, str]] = []
    for word, positions in idx.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for p in positions:
            if isinstance(p, int):
                pairs.append((p, word))
    if not pairs:
        return ""
    pairs.sort(key=lambda x: x[0])
    return " ".join(word for _, word in pairs)


async def batch_search_openalex(
    queries: list[str],
    max_per_query: int = 5,
    concurrency: int = 4,
    *,
    mailto: str | None = None,
) -> dict[str, list[Paper]]:
    """Run multiple OpenAlex queries in parallel with bounded concurrency.

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
                    return q, await search_openalex(
                        q, max_per_query, client=client, mailto=mailto
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("OpenAlex query %r failed: %s", q, e)
                    return q, []

        pairs = await asyncio.gather(*[one(q) for q in queries])
    return dict(pairs)


__all__ = ["OPENALEX_API_URL", "batch_search_openalex", "search_openalex"]
