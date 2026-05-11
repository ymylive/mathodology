"""PDF / OA paper retrieval and text extraction.

A thin async layer that:
  - resolves a Paper (with arxiv_id or doi) to a fetchable PDF/HTML URL
    (arXiv direct + Unpaywall API for DOIs)
  - downloads + extracts the text via pdfplumber (PDF) or trafilatura (HTML)
  - persists the top N papers' text under runs/<run_id>/papers/NN.md for
    the Writer to consume

Best-effort throughout: any failure returns None / [] / "" so the Searcher
keeps running and SearchFindings.paper_fulltext_paths just stays empty.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Literal

import httpx
import pdfplumber
import trafilatura
from mm_contracts import Paper

_log = logging.getLogger(__name__)

ARXIV_PDF_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}.pdf"
UNPAYWALL_API_URL = "https://api.unpaywall.org/v2/{doi}"


async def find_pdf_url(
    paper: Paper,
    *,
    mailto: str | None = None,
    timeout: float = 10.0,  # noqa: ASYNC109 — applied to the httpx client, not an asyncio primitive
) -> str | None:
    """Resolve a Paper to a fetchable PDF/HTML URL or None.

    Resolution order:
      1. arXiv ID present → arxiv.org/pdf/<id>.pdf (always available)
      2. DOI present → Unpaywall API → best_oa_location.url_for_pdf
      3. None
    """
    if paper.arxiv_id:
        return ARXIV_PDF_TEMPLATE.format(arxiv_id=paper.arxiv_id)
    if not paper.doi:
        return None

    # Unpaywall recommends including a mailto for the polite pool. Without
    # one we still get a response, just with stricter rate limits.
    params: dict[str, str] = {}
    if mailto:
        params["email"] = mailto
    url = UNPAYWALL_API_URL.format(doi=paper.doi)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001 — best-effort
        _log.warning("Unpaywall lookup failed for %s: %s", paper.doi, e)
        return None

    if not isinstance(data, dict):
        return None
    oa = data.get("best_oa_location")
    if not isinstance(oa, dict):
        return None
    pdf_url = oa.get("url_for_pdf")
    return pdf_url if isinstance(pdf_url, str) and pdf_url.strip() else None


async def fetch_and_extract(
    url: str,
    *,
    timeout: float = 15.0,  # noqa: ASYNC109 — applied to httpx, not an asyncio primitive
    max_bytes: int = 20_000_000,
) -> tuple[str | None, Literal["trafilatura", "pdfplumber", "none"]]:
    """Fetch URL and return (extracted_text, parser_name).

    text=None signals failure for any reason (network, oversize, parse,
    or empty extraction). The caller never raises — the Searcher's
    enrichment phase is best-effort. ``max_bytes`` rejects oversized
    bodies to bound memory.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — best-effort
        _log.warning("fetch_and_extract: HTTP failure for %s: %s", url, e)
        return None, "none"

    content = r.content
    if len(content) > max_bytes:
        _log.warning(
            "fetch_and_extract: oversized response %d bytes for %s; rejecting",
            len(content), url,
        )
        return None, "none"

    ctype = (r.headers.get("content-type") or "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                pages_text = [p.extract_text() or "" for p in pdf.pages]
            text = "\n\n".join(t for t in pages_text if t.strip())
        except Exception as e:  # noqa: BLE001
            _log.warning("fetch_and_extract: pdfplumber failed for %s: %s", url, e)
            return None, "none"
        return (text, "pdfplumber") if text.strip() else (None, "none")

    # HTML / other text content → trafilatura
    try:
        text = trafilatura.extract(
            content,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            url=url,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("fetch_and_extract: trafilatura failed for %s: %s", url, e)
        return None, "none"

    if not text or not text.strip():
        return None, "none"
    return text, "trafilatura"


async def batch_enrich_papers(
    papers: list[Paper],
    runs_papers_dir: Path,
    top_n: int = 3,
    *,
    mailto: str | None = None,
    concurrency: int = 3,
) -> list[str]:
    """Enrich the top N papers concurrently and persist successes.

    For each paper in papers[:top_n], in parallel (bounded by ``concurrency``):
      1. Resolve a PDF/HTML URL via find_pdf_url.
      2. Fetch + extract via fetch_and_extract.
      3. On success, write the text to ``runs_papers_dir / f"{idx:02d}.md"``
         where ``idx`` is the 1-based original position in papers[:top_n].

    Returns a list of relative paths under runs_papers_dir.parent, in
    original index order, suitable for SearchFindings.paper_fulltext_paths.
    Failed papers are not represented in the output.
    """
    if not papers:
        return []
    # Create directory synchronously (I/O is local).
    runs_papers_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    sem = asyncio.Semaphore(concurrency)

    async def enrich_one(idx: int, paper: Paper) -> tuple[int, str | None]:
        async with sem:
            pdf_url = await find_pdf_url(paper, mailto=mailto)
            if not pdf_url:
                return idx, None
            text, parser = await fetch_and_extract(pdf_url)
            if not text:
                _log.warning(
                    "enrich: extraction empty for paper #%d (%s)", idx, pdf_url
                )
                return idx, None
            rel_path = f"papers/{idx:02d}.md"
            (runs_papers_dir / f"{idx:02d}.md").write_text(text, encoding="utf-8")
            _log.info(
                "enrich: paper #%d via %s (%d chars)", idx, parser, len(text)
            )
            return idx, rel_path

    results = await asyncio.gather(
        *(enrich_one(i, p) for i, p in enumerate(papers[:top_n], start=1))
    )
    return [rel for _, rel in sorted(results) if rel is not None]


__all__ = [
    "ARXIV_PDF_TEMPLATE",
    "UNPAYWALL_API_URL",
    "batch_enrich_papers",
    "fetch_and_extract",
    "find_pdf_url",
]
