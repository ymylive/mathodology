"""Tests for tools/pdf.py — offline via pytest-httpx."""

from __future__ import annotations

from pathlib import Path

from agent_worker.tools.pdf import batch_enrich_papers, fetch_and_extract, find_pdf_url
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


# Minimal valid PDF byte stream that pdfplumber accepts. We do NOT actually
# parse it — pdfplumber.open is monkey-patched in the test below.
_FAKE_PDF_BYTES = b"%PDF-1.4\n%fake\n"


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdf:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages

    def __enter__(self) -> _FakePdf:
        return self

    def __exit__(self, *_: object) -> None:
        return None


async def test_fetch_and_extract_pdf(httpx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        content=_FAKE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )
    fake = _FakePdf([_FakePage("Hello, world."), _FakePage("Methods: ...")])
    monkeypatch.setattr(
        "agent_worker.tools.pdf.pdfplumber.open", lambda _: fake
    )

    text, parser = await fetch_and_extract("http://example.com/p.pdf")
    assert "Hello, world." in text
    assert "Methods" in text
    assert parser == "pdfplumber"


async def test_fetch_and_extract_html_uses_trafilatura(httpx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    html = b"<html><body><article>Main content here.</article></body></html>"
    httpx_mock.add_response(
        content=html, headers={"content-type": "text/html; charset=utf-8"}
    )
    monkeypatch.setattr(
        "agent_worker.tools.pdf.trafilatura.extract",
        lambda body, **_: "Main content here.",
    )

    text, parser = await fetch_and_extract("http://example.com/article")
    assert text == "Main content here."
    assert parser == "trafilatura"


async def test_fetch_and_extract_rejects_oversized_response(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        content=b"x" * (21 * 1024 * 1024),
        headers={"content-type": "application/pdf"},
    )
    text, parser = await fetch_and_extract(
        "http://example.com/huge.pdf", max_bytes=20_000_000
    )
    assert text is None
    assert parser == "none"


async def test_fetch_and_extract_returns_none_on_timeout(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    import httpx
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    text, parser = await fetch_and_extract("http://example.com/slow.pdf")
    assert text is None
    assert parser == "none"


async def test_fetch_and_extract_returns_none_when_extractor_returns_empty(
    httpx_mock, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    httpx_mock.add_response(
        content=_FAKE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )
    fake = _FakePdf([_FakePage("")])
    monkeypatch.setattr(
        "agent_worker.tools.pdf.pdfplumber.open", lambda _: fake
    )

    text, parser = await fetch_and_extract("http://example.com/empty.pdf")
    assert text is None
    assert parser == "none"


async def test_batch_enrich_papers_writes_files_for_arxiv_sources(
    tmp_path: Path, httpx_mock, monkeypatch
) -> None:
    # Three arXiv papers — no Unpaywall calls, direct PDF URLs.
    papers = [_arxiv_paper(f"2401.{i:05d}") for i in range(1, 4)]
    # Each pdf fetch returns a parseable PDF byte stream.
    for _ in papers:
        httpx_mock.add_response(
            content=b"%PDF-1.4\n",
            headers={"content-type": "application/pdf"},
        )
    monkeypatch.setattr(
        "agent_worker.tools.pdf.pdfplumber.open",
        lambda _: _FakePdf([_FakePage("paper body")]),
    )

    runs_papers_dir = tmp_path / "papers"
    paths = await batch_enrich_papers(
        papers, runs_papers_dir=runs_papers_dir, top_n=3
    )
    assert paths == ["papers/01.md", "papers/02.md", "papers/03.md"]
    for rel in paths:
        assert (tmp_path / rel).read_text("utf-8") == "paper body"


async def test_batch_enrich_papers_caps_at_top_n(
    tmp_path: Path, httpx_mock, monkeypatch
) -> None:
    """Given 5 papers and top_n=3, only the first 3 are processed."""
    papers = [_arxiv_paper(f"2401.{i:05d}") for i in range(1, 6)]
    for _ in range(3):
        httpx_mock.add_response(
            content=b"%PDF-1.4\n",
            headers={"content-type": "application/pdf"},
        )
    monkeypatch.setattr(
        "agent_worker.tools.pdf.pdfplumber.open",
        lambda _: _FakePdf([_FakePage("body")]),
    )

    paths = await batch_enrich_papers(
        papers, runs_papers_dir=tmp_path / "papers", top_n=3
    )
    assert len(paths) == 3


async def test_batch_enrich_papers_skips_failed_papers(
    tmp_path: Path, httpx_mock, monkeypatch
) -> None:
    """Paper 2 fails; results contain only 01 and 03 with correct numbering."""
    papers = [_arxiv_paper(f"2401.{i:05d}") for i in range(1, 4)]
    # Paper 1: success
    httpx_mock.add_response(
        content=b"%PDF-1.4\n", headers={"content-type": "application/pdf"}
    )
    # Paper 2: HTTP failure
    httpx_mock.add_response(status_code=503)
    # Paper 3: success
    httpx_mock.add_response(
        content=b"%PDF-1.4\n", headers={"content-type": "application/pdf"}
    )
    monkeypatch.setattr(
        "agent_worker.tools.pdf.pdfplumber.open",
        lambda _: _FakePdf([_FakePage("body")]),
    )

    paths = await batch_enrich_papers(
        papers, runs_papers_dir=tmp_path / "papers", top_n=3, concurrency=1
    )
    # Numbering matches original index (01, 03) — 02 dropped.
    assert paths == ["papers/01.md", "papers/03.md"]


async def test_batch_enrich_papers_returns_empty_when_all_fail(
    tmp_path: Path, httpx_mock
) -> None:
    papers = [_arxiv_paper(f"2401.{i:05d}") for i in range(1, 4)]
    for _ in range(3):
        httpx_mock.add_response(status_code=503)
    paths = await batch_enrich_papers(
        papers, runs_papers_dir=tmp_path / "papers", top_n=3
    )
    assert paths == []


async def test_batch_enrich_papers_empty_list_input() -> None:
    paths = await batch_enrich_papers([], runs_papers_dir=Path("/nonexistent"))
    assert paths == []


async def test_find_pdf_url_extracts_doi_from_url_when_doi_field_missing(
    httpx_mock,  # type: ignore[no-untyped-def]
) -> None:
    """LLM-synthesized SearchFindings often puts DOI in url but leaves doi=null.
    find_pdf_url must still resolve via Unpaywall in that case."""
    paper = Paper(
        title="Bank Queueing Study",
        authors=["X. Smith"],
        abstract="abs",
        url="https://doi.org/10.1287/msom.5.2.79.16071",
        # NB: no doi= and no arxiv_id=  — both null
    )
    httpx_mock.add_response(
        json={
            "best_oa_location": {
                "url_for_pdf": "https://example.org/oa.pdf",
            }
        }
    )
    url = await find_pdf_url(paper, mailto="bot@example.com")
    assert url == "https://example.org/oa.pdf"

    req = httpx_mock.get_request()
    assert "10.1287/msom.5.2.79.16071" in str(req.url)


async def test_find_pdf_url_extracts_doi_from_http_url_prefix(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """Same fallback works for plain http:// doi.org URLs too (rare but valid)."""
    paper = Paper(
        title="Old Paper", authors=[], abstract="",
        url="http://doi.org/10.1234/legacy",
    )
    httpx_mock.add_response(json={"best_oa_location": {"url_for_pdf": "https://x/o.pdf"}})
    url = await find_pdf_url(paper, mailto="bot@example.com")
    assert url == "https://x/o.pdf"


async def test_find_pdf_url_ignores_non_doi_urls() -> None:
    """A url that's not a doi.org URL doesn't trigger Unpaywall (no mock needed)."""
    paper = Paper(
        title="Blog Post", authors=[], abstract="",
        url="https://blog.example/some-post",
    )
    url = await find_pdf_url(paper, mailto="bot@example.com")
    assert url is None


async def test_find_pdf_url_prefers_paper_doi_field_over_url(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """When both doi field and url are set, doi field wins (deterministic)."""
    paper = Paper(
        title="Both", authors=[], abstract="",
        url="https://doi.org/10.URL/wrong",
        doi="10.FIELD/right",
    )
    httpx_mock.add_response(json={"best_oa_location": {"url_for_pdf": "https://x.pdf"}})
    url = await find_pdf_url(paper, mailto="bot@example.com")
    assert url == "https://x.pdf"
    req = httpx_mock.get_request()
    assert "10.FIELD/right" in str(req.url)
    assert "10.URL/wrong" not in str(req.url)
