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

import logging

import httpx
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


__all__ = [
    "ARXIV_PDF_TEMPLATE",
    "UNPAYWALL_API_URL",
    "find_pdf_url",
]
