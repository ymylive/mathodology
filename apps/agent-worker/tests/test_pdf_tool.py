"""Tests for tools/pdf.py — offline via pytest-httpx."""

from __future__ import annotations

from agent_worker.tools.pdf import find_pdf_url
from mm_contracts import Paper


def _arxiv_paper(arxiv_id: str = "2312.01234") -> Paper:
    return Paper(
        title="Arxiv Paper",
        authors=["A. B."],
        abstract="abs",
        url=f"http://arxiv.org/abs/{arxiv_id}",
        arxiv_id=arxiv_id,
    )


def _doi_paper(doi: str = "10.1234/example.2024.001") -> Paper:
    return Paper(
        title="Crossref Paper",
        authors=["C. D."],
        abstract="abs",
        url=f"https://doi.org/{doi}",
        doi=doi,
    )


async def test_find_pdf_url_returns_arxiv_pdf_for_arxiv_paper() -> None:
    """arXiv papers are resolved without contacting Unpaywall."""
    url = await find_pdf_url(_arxiv_paper("2312.01234"))
    assert url == "https://arxiv.org/pdf/2312.01234.pdf"


async def test_find_pdf_url_returns_oa_url_when_unpaywall_has_one(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        json={
            "best_oa_location": {
                "url_for_pdf": "https://example.org/paper.pdf",
            }
        }
    )
    url = await find_pdf_url(_doi_paper("10.1/abc"), mailto="bot@example.com")
    assert url == "https://example.org/paper.pdf"
    request = httpx_mock.get_request()
    assert "10.1/abc" in str(request.url)
    assert "email=bot%40example.com" in str(request.url)


async def test_find_pdf_url_returns_none_when_unpaywall_has_no_oa(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(json={"best_oa_location": None})
    url = await find_pdf_url(_doi_paper("10.1/no-oa"), mailto="bot@example.com")
    assert url is None


async def test_find_pdf_url_returns_none_when_paper_has_neither_arxiv_nor_doi() -> None:
    paper = Paper(
        title="Web Hit",
        authors=[],
        abstract="",
        url="https://blog.example/post",
    )
    url = await find_pdf_url(paper, mailto="bot@example.com")
    assert url is None


async def test_find_pdf_url_returns_none_when_unpaywall_5xx(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(status_code=503)
    url = await find_pdf_url(_doi_paper(), mailto="bot@example.com")
    assert url is None
