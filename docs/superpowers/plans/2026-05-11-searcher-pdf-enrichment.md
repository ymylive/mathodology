# Searcher PDF Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich `SearchFindings` with full-text content for the top 3 papers so the Writer can cite specific equations, methodology details, and numerical results instead of generic "according to [12]".

**Architecture:** Add a new `tools/pdf.py` module that resolves Paper → PDF URL (arXiv direct + Unpaywall for DOIs) and extracts text via trafilatura/pdfplumber. Wire two new phases into `SearcherAgent.run_for`: (3.5) `batch_enrich_papers` persists top-3 papers' text to `runs/<id>/papers/NN.md`; (3.6) `_compact_oversized_papers` invokes an inline LLM call to compact files over 24k chars while preserving source language. Writer reads paths from `SearchFindings.paper_fulltext_paths` and inlines content into its prompt with a 32k-char paragraph-boundary soft-truncation safety net. Failure-tolerant at every step — empty `paper_fulltext_paths` reverts cleanly to v0.5.3 behavior.

**Tech Stack:** Python 3.11, httpx (async HTTP), trafilatura (HTML→text), pdfplumber (PDF→text), Pydantic v2 (contracts), pytest + pytest-httpx (tests).

**Spec:** `docs/superpowers/specs/2026-05-11-searcher-pdf-enrichment-design.md`

---

## Task 1: Add dependencies and extend `SearchFindings` contract

**Files:**
- Modify: `apps/agent-worker/pyproject.toml` (add deps)
- Modify: `apps/agent-worker/uv.lock` (auto-regenerated)
- Modify: `packages/py-contracts/src/mm_contracts/agent_io.py` (`SearchFindings` model)
- Modify: `packages/ts-contracts/src/index.ts` (TS mirror)
- Test: `apps/agent-worker/tests/test_contracts_searchfindings.py` (new minimal test for the new field)

- [ ] **Step 1: Write the failing test**

Create `apps/agent-worker/tests/test_contracts_searchfindings.py`:

```python
"""Backward-compat check for the SearchFindings.paper_fulltext_paths field."""

from __future__ import annotations

import pytest
from mm_contracts import SearchFindings
from pydantic import ValidationError


def test_searchfindings_without_paper_fulltext_paths_defaults_to_empty() -> None:
    sf = SearchFindings(queries=["q"], papers=[], key_findings=[], datasets_mentioned=[])
    assert sf.paper_fulltext_paths == []


def test_searchfindings_accepts_paper_fulltext_paths() -> None:
    sf = SearchFindings(
        queries=["q"],
        papers=[],
        key_findings=[],
        datasets_mentioned=[],
        paper_fulltext_paths=["papers/01.md", "papers/02.md", "papers/03.md"],
    )
    assert sf.paper_fulltext_paths == ["papers/01.md", "papers/02.md", "papers/03.md"]


def test_searchfindings_caps_paper_fulltext_paths_at_three() -> None:
    with pytest.raises(ValidationError):
        SearchFindings(
            queries=["q"],
            papers=[],
            key_findings=[],
            datasets_mentioned=[],
            paper_fulltext_paths=[f"papers/0{i}.md" for i in range(1, 5)],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/agent-worker && uv run pytest tests/test_contracts_searchfindings.py -v`
Expected: FAIL on `paper_fulltext_paths` — `SearchFindings` does not have that field yet.

- [ ] **Step 3: Add the contract field**

Edit `packages/py-contracts/src/mm_contracts/agent_io.py`, find class `SearchFindings`, append (preserving existing fields):

```python
class SearchFindings(BaseModel):
    """Searcher agent output: curated papers + synthesized key findings."""

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(default_factory=list, max_length=10)
    papers: list[Paper] = Field(default_factory=list, max_length=15)
    key_findings: list[str] = Field(default_factory=list, max_length=10)
    datasets_mentioned: list[str] = Field(default_factory=list, max_length=10)
    # NEW: relative paths under runs/<run_id>/ pointing at extracted full-text
    # markdown for the top 3 cited papers. Empty when enrichment did not run
    # or every paper's PDF was unreachable / parse failed. The Writer reads
    # these to ground specific citations; failure to load is non-fatal.
    paper_fulltext_paths: list[str] = Field(default_factory=list, max_length=3)
```

- [ ] **Step 4: Mirror in TS contracts**

Edit `packages/ts-contracts/src/index.ts`, find the existing `SearchFindings` interface (or add one if missing), append:

```ts
export interface SearchFindings {
  queries?: string[];
  papers?: Paper[];
  key_findings?: string[];
  datasets_mentioned?: string[];
  paper_fulltext_paths?: string[];
}
```

If `SearchFindings` already exists in `index.ts`, only add the `paper_fulltext_paths?: string[];` line. Do NOT alter the existing fields.

- [ ] **Step 5: Add the new Python dependencies**

Run: `cd apps/agent-worker && uv add 'trafilatura>=1.12' 'pdfplumber>=0.11'`
Expected: writes to `pyproject.toml`, regenerates `uv.lock`. `uv` resolves transitive deps (`pdfminer.six`, `lxml`, `justext`, `charset-normalizer`).

- [ ] **Step 6: Run test to verify it passes**

Run: `cd apps/agent-worker && uv run pytest tests/test_contracts_searchfindings.py -v`
Expected: 3 passed.

- [ ] **Step 7: Run full repo lint and the full agent-worker test suite to confirm no regression**

Run: `cd /Users/cornna/project/math_agent && uvx ruff check .`
Expected: `All checks passed!`

Run: `cd apps/agent-worker && uv run pytest --tb=short -q`
Expected: All existing tests still pass (290 baseline + the 3 new ones = 293).

- [ ] **Step 8: Commit**

```bash
git add packages/py-contracts/src/mm_contracts/agent_io.py packages/ts-contracts/src/index.ts apps/agent-worker/pyproject.toml apps/agent-worker/uv.lock apps/agent-worker/tests/test_contracts_searchfindings.py
git commit -m "feat(contracts): add SearchFindings.paper_fulltext_paths (top-3 cap)

Backward compatible (default empty). TS mirror optional. Adds trafilatura
and pdfplumber as agent-worker deps in preparation for the PDF enrichment
tool."
```

---

## Task 2: `tools/pdf.py::find_pdf_url` — resolve Paper to a PDF/HTML URL

**Files:**
- Create: `apps/agent-worker/src/agent_worker/tools/pdf.py`
- Create: `apps/agent-worker/tests/test_pdf_tool.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/agent-worker/tests/test_pdf_tool.py`:

```python
"""Tests for tools/pdf.py — offline via pytest-httpx."""

from __future__ import annotations

import pytest
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


async def test_find_pdf_url_returns_oa_url_when_unpaywall_has_one(httpx_mock) -> None:
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


async def test_find_pdf_url_returns_none_when_unpaywall_has_no_oa(httpx_mock) -> None:
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


async def test_find_pdf_url_returns_none_when_unpaywall_5xx(httpx_mock) -> None:
    httpx_mock.add_response(status_code=503)
    url = await find_pdf_url(_doi_paper(), mailto="bot@example.com")
    assert url is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-worker && uv run pytest tests/test_pdf_tool.py -v`
Expected: All FAIL — `tools.pdf` does not exist.

- [ ] **Step 3: Implement `find_pdf_url`**

Create `apps/agent-worker/src/agent_worker/tools/pdf.py`:

```python
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
from typing import Literal

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
        _log.info("Unpaywall lookup failed for %s: %s", paper.doi, e)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-worker && uv run pytest tests/test_pdf_tool.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/agent-worker/src/agent_worker/tools/pdf.py apps/agent-worker/tests/test_pdf_tool.py
git commit -m "feat(pdf): find_pdf_url resolves Paper to arXiv or Unpaywall URL"
```

---

## Task 3: `tools/pdf.py::fetch_and_extract` — download + extract text

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/tools/pdf.py` (append)
- Modify: `apps/agent-worker/tests/test_pdf_tool.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `apps/agent-worker/tests/test_pdf_tool.py`:

```python
from agent_worker.tools.pdf import fetch_and_extract


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

    def __enter__(self) -> "_FakePdf":
        return self

    def __exit__(self, *_: object) -> None:
        return None


async def test_fetch_and_extract_pdf(httpx_mock, monkeypatch) -> None:
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


async def test_fetch_and_extract_html_uses_trafilatura(httpx_mock, monkeypatch) -> None:
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


async def test_fetch_and_extract_rejects_oversized_response(httpx_mock) -> None:
    httpx_mock.add_response(
        content=b"x" * (21 * 1024 * 1024),
        headers={"content-type": "application/pdf"},
    )
    text, parser = await fetch_and_extract(
        "http://example.com/huge.pdf", max_bytes=20_000_000
    )
    assert text is None
    assert parser == "none"


async def test_fetch_and_extract_returns_none_on_timeout(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    text, parser = await fetch_and_extract("http://example.com/slow.pdf")
    assert text is None
    assert parser == "none"


async def test_fetch_and_extract_returns_none_when_extractor_returns_empty(
    httpx_mock, monkeypatch
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-worker && uv run pytest tests/test_pdf_tool.py -v`
Expected: New tests FAIL — `fetch_and_extract` does not exist.

- [ ] **Step 3: Implement `fetch_and_extract`**

Append to `apps/agent-worker/src/agent_worker/tools/pdf.py`:

```python
import io

import pdfplumber
import trafilatura


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
        _log.info("fetch_and_extract: HTTP failure for %s: %s", url, e)
        return None, "none"

    content = r.content
    if len(content) > max_bytes:
        _log.info(
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
            _log.info("fetch_and_extract: pdfplumber failed for %s: %s", url, e)
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
        _log.info("fetch_and_extract: trafilatura failed for %s: %s", url, e)
        return None, "none"

    if not text or not text.strip():
        return None, "none"
    return text, "trafilatura"
```

Update `__all__` at the bottom of `pdf.py`:

```python
__all__ = [
    "ARXIV_PDF_TEMPLATE",
    "UNPAYWALL_API_URL",
    "fetch_and_extract",
    "find_pdf_url",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-worker && uv run pytest tests/test_pdf_tool.py -v`
Expected: 10 passed (5 from Task 2 + 5 new).

- [ ] **Step 5: Commit**

```bash
git add apps/agent-worker/src/agent_worker/tools/pdf.py apps/agent-worker/tests/test_pdf_tool.py
git commit -m "feat(pdf): fetch_and_extract via pdfplumber (PDFs) or trafilatura (HTML)"
```

---

## Task 4: `tools/pdf.py::batch_enrich_papers` — orchestrate top-N enrichment

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/tools/pdf.py` (append)
- Modify: `apps/agent-worker/tests/test_pdf_tool.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `apps/agent-worker/tests/test_pdf_tool.py`:

```python
from pathlib import Path

from agent_worker.tools.pdf import batch_enrich_papers


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-worker && uv run pytest tests/test_pdf_tool.py -v`
Expected: 5 new tests FAIL — `batch_enrich_papers` not implemented.

- [ ] **Step 3: Implement `batch_enrich_papers`**

Append to `apps/agent-worker/src/agent_worker/tools/pdf.py`:

```python
import asyncio
from pathlib import Path


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
    runs_papers_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)

    async def enrich_one(idx: int, paper: Paper) -> tuple[int, str | None]:
        async with sem:
            pdf_url = await find_pdf_url(paper, mailto=mailto)
            if not pdf_url:
                return idx, None
            text, parser = await fetch_and_extract(pdf_url)
            if not text:
                _log.info(
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
```

Update `__all__`:

```python
__all__ = [
    "ARXIV_PDF_TEMPLATE",
    "UNPAYWALL_API_URL",
    "batch_enrich_papers",
    "fetch_and_extract",
    "find_pdf_url",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-worker && uv run pytest tests/test_pdf_tool.py -v`
Expected: 15 passed (5 + 5 + 5).

- [ ] **Step 5: Commit**

```bash
git add apps/agent-worker/src/agent_worker/tools/pdf.py apps/agent-worker/tests/test_pdf_tool.py
git commit -m "feat(pdf): batch_enrich_papers orchestrates top-N enrichment"
```

---

## Task 5: SearcherAgent compaction method

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/agents/searcher.py` (add method + constants)
- Create: `apps/agent-worker/tests/test_searcher_compaction.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/agent-worker/tests/test_searcher_compaction.py`:

```python
"""Tests for SearcherAgent._compact_oversized_papers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from agent_worker.agents import SearcherAgent
from agent_worker.agents.searcher import COMPACT_THRESHOLD_CHARS


class _FakeEmitter:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.events: list[tuple[str, dict, str | None]] = []

    async def emit(self, kind, payload=None, agent=None):  # noqa: ANN001
        self.events.append((kind, payload or {}, agent))


class _ScriptedGateway:
    def __init__(self, response: str | Exception) -> None:
        self._response = response

    async def stream_completion(self, **_: object) -> AsyncIterator[str]:
        if isinstance(self._response, Exception):
            raise self._response
        yield self._response

    async def close(self) -> None:
        pass


def _make_agent(response):  # noqa: ANN001
    return SearcherAgent(_ScriptedGateway(response), _FakeEmitter())  # type: ignore[arg-type]


async def test_compact_skips_files_under_threshold(tmp_path: Path) -> None:
    """Files under the threshold are not touched and the LLM is not called."""
    agent = _make_agent(RuntimeError("LLM must not be called for short files"))
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    short = "short content" * 100  # well below 24k chars
    (runs_papers_dir / "01.md").write_text(short, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert paths == ["papers/01.md"]
    assert (runs_papers_dir / "01.md").read_text("utf-8") == short


async def test_compact_overwrites_oversized_file_with_llm_output(tmp_path: Path) -> None:
    compacted = "## Methods\n\n" + ("dense " * 100)
    agent = _make_agent(compacted)
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    long_text = "x" * (COMPACT_THRESHOLD_CHARS + 1000)
    (runs_papers_dir / "01.md").write_text(long_text, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert paths == ["papers/01.md"]
    assert (runs_papers_dir / "01.md").read_text("utf-8") == compacted


async def test_compact_keeps_raw_on_llm_failure(tmp_path: Path) -> None:
    agent = _make_agent(RuntimeError("provider 502"))
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    long_text = "x" * (COMPACT_THRESHOLD_CHARS + 1000)
    (runs_papers_dir / "01.md").write_text(long_text, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert paths == ["papers/01.md"]
    # File untouched.
    assert (runs_papers_dir / "01.md").read_text("utf-8") == long_text


async def test_compact_keeps_raw_when_llm_output_too_short(tmp_path: Path) -> None:
    agent = _make_agent("tiny")  # << COMPACT_MIN_OUTPUT_CHARS
    runs_papers_dir = tmp_path / "papers"
    runs_papers_dir.mkdir()
    long_text = "x" * (COMPACT_THRESHOLD_CHARS + 1000)
    (runs_papers_dir / "01.md").write_text(long_text, encoding="utf-8")

    paths = await agent._compact_oversized_papers(runs_papers_dir, ["papers/01.md"])
    assert (runs_papers_dir / "01.md").read_text("utf-8") == long_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-worker && uv run pytest tests/test_searcher_compaction.py -v`
Expected: All FAIL — `_compact_oversized_papers` and `COMPACT_THRESHOLD_CHARS` not defined.

- [ ] **Step 3: Add constants + method to SearcherAgent**

Edit `apps/agent-worker/src/agent_worker/agents/searcher.py`. Just BEFORE the `class SearcherAgent` declaration, add module-level constants:

```python
# --- PDF enrichment / compaction tuning -------------------------------------
# Raw extracted text above this character count triggers an LLM compaction
# pass. ~24k chars ≈ 6k tokens, leaving Writer headroom for 3 papers.
COMPACT_THRESHOLD_CHARS = 24_000
# Output ceiling for the compaction LLM call. Used in the prompt as a hint;
# we accept output up to ~32k before Writer-side soft truncation kicks in.
COMPACT_TARGET_CHARS = 24_000
# Below this, treat compaction output as a failure and keep the raw file.
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
```

Then inside `class SearcherAgent` (after `_refine_queries`, before `_synthesize` works), add the two methods:

```python
async def _compact_oversized_papers(
    self, runs_papers_dir: Path, paths: list[str]
) -> list[str]:
    """Compact any persisted paper text exceeding COMPACT_THRESHOLD_CHARS.

    Overwrites in place. Compaction failure leaves the raw file untouched
    — the Writer side has its own char-budget soft truncation as a safety
    net.

    Returns the same paths argument unchanged (compaction never drops a
    paper; it either improves the file or leaves it alone).
    """
    # runs_papers_dir is the .../papers/ subdir. Resolve relative paths
    # (e.g. "papers/01.md") against its parent so the math works out the
    # same as how Writer reconstructs the path.
    run_dir = runs_papers_dir.parent
    for rel_path in paths:
        abs_path = run_dir / rel_path
        try:
            raw = abs_path.read_text("utf-8")
        except OSError as e:  # missing / unreadable
            _log = self._log if hasattr(self, "_log") else None
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
        text = await self._stream_and_collect(model, messages)
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
```

Note: `_stream_and_collect` in this codebase requests `response_format={"type": "json_object"}`. The compaction output is markdown, not JSON. If `_stream_and_collect` always sets json_object, override here. Inspect `_stream_and_collect` (in searcher.py) — if it forces JSON, add a temporary unset by writing a small `_stream_and_collect_text` helper, or relax the response_format for non-JSON-needed calls. Read its body before editing; if it's hardcoded, the cleanest fix is to add an optional `response_format: dict | None = None` parameter defaulting to `{"type": "json_object"}` (current behavior preserved) and pass `None` from `_compact_one`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-worker && uv run pytest tests/test_searcher_compaction.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/agent-worker/src/agent_worker/agents/searcher.py apps/agent-worker/tests/test_searcher_compaction.py
git commit -m "feat(searcher): compaction LLM pass for oversized paper text

24k-char threshold triggers a single LLM call that preserves formulas /
methodology / numerical results and drops boilerplate. Source-language
preserving (Chinese stays Chinese). Failure keeps the raw file —
Writer-side soft truncation handles the long case."
```

---

## Task 6: Wire enrichment + compaction into `SearcherAgent.run_for`

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/agents/searcher.py` (constructor, run_for)
- Modify: `apps/agent-worker/src/agent_worker/pipeline.py` (pass `runs_dir` when constructing the SearcherAgent)
- Modify: `apps/agent-worker/tests/test_searcher_agent.py` (extend existing tests)

- [ ] **Step 1: Write the failing tests**

Append to `apps/agent-worker/tests/test_searcher_agent.py` (the file you extended in Task 1 of the prior PR — model it after `test_searcher_emits_expected_events`):

```python
from pathlib import Path

from agent_worker.tools.pdf import batch_enrich_papers as _real_batch  # noqa: F401


async def test_searcher_populates_paper_fulltext_paths(
    monkeypatch: pytest.MonkeyPatch,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    tmp_path: Path,
) -> None:
    """When runs_dir is provided and enrichment succeeds, paths land on SearchFindings."""
    papers = _sample_papers()

    async def fake_batch(queries, max_per_query=5, concurrency=2):  # noqa: ANN001
        return {q: papers for q in queries[:1]} | {q: [] for q in queries[1:]}

    async def fake_empty(queries, **_):  # noqa: ANN001, ANN003
        return {q: [] for q in queries}

    async def fake_enrich(papers_arg, runs_papers_dir, top_n=3, **_):  # noqa: ANN001, ANN003
        # Confirm the Searcher chose the right cap.
        assert top_n == 3
        runs_papers_dir.mkdir(parents=True, exist_ok=True)
        (runs_papers_dir / "01.md").write_text("body 1", encoding="utf-8")
        return ["papers/01.md"]

    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_arxiv", fake_batch
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_openalex", fake_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_crossref", fake_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_web", fake_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_enrich_papers", fake_enrich
    )

    gateway = _FakeGateway([_FINDINGS_JSON])
    emitter = _FakeEmitter()

    agent = SearcherAgent(gateway, emitter, runs_dir=tmp_path)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert out.paper_fulltext_paths == ["papers/01.md"]
    paper_file = tmp_path / str(emitter.run_id) / "papers" / "01.md"
    assert paper_file.exists()


async def test_searcher_skips_enrichment_when_no_papers(
    monkeypatch: pytest.MonkeyPatch,
    problem: ProblemInput,
    analysis: AnalyzerOutput,
    tmp_path: Path,
) -> None:
    """No papers → no enrichment call → empty paper_fulltext_paths."""
    async def fake_batch_empty(queries, max_per_query=5, concurrency=2):  # noqa: ANN001
        return {q: [] for q in queries}

    async def fake_empty(queries, **_):  # noqa: ANN001, ANN003
        return {q: [] for q in queries}

    enrich_called = False

    async def fake_enrich(*_a, **_kw):  # noqa: ANN001, ANN003
        nonlocal enrich_called
        enrich_called = True
        return []

    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_arxiv", fake_batch_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_openalex", fake_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_crossref", fake_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_search_web", fake_empty
    )
    monkeypatch.setattr(
        "agent_worker.agents.searcher.batch_enrich_papers", fake_enrich
    )

    class _NoopGateway:
        async def stream_completion(self, **_: object) -> AsyncIterator[str]:
            raise AssertionError("LLM must not be called")
            yield ""

        async def close(self) -> None:
            pass

    emitter = _FakeEmitter()
    agent = SearcherAgent(_NoopGateway(), emitter, runs_dir=tmp_path)  # type: ignore[arg-type]
    out = await agent.run_for(problem, analysis)

    assert out.papers == []
    assert out.paper_fulltext_paths == []
    assert enrich_called is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-worker && uv run pytest tests/test_searcher_agent.py::test_searcher_populates_paper_fulltext_paths tests/test_searcher_agent.py::test_searcher_skips_enrichment_when_no_papers -v`
Expected: FAIL — `runs_dir` constructor kwarg unknown OR `batch_enrich_papers` import path missing OR `paper_fulltext_paths` not populated.

- [ ] **Step 3: Update SearcherAgent constructor to accept `runs_dir`**

Edit `apps/agent-worker/src/agent_worker/agents/searcher.py`. Find the `__init__` of `SearcherAgent` and add `runs_dir: Path | None = None` parameter. Store on `self._runs_dir`. Add `from pathlib import Path` to imports if not present.

```python
class SearcherAgent:
    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        prompt_version: str = "v1",
        run_effort: ReasoningEffort = "medium",
        long_context: bool = False,
        model_override: str | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        # ... existing assignments ...
        self._runs_dir = runs_dir
```

Add at the top of the file:
```python
from agent_worker.tools.pdf import batch_enrich_papers
```

- [ ] **Step 4: Wire Phase 3.5 + 3.6 into `run_for`**

In `run_for`, locate the line where `findings = await self._synthesize(...)` is assigned (or where the empty-skip branch sets `findings = SearchFindings(queries=queries)`). After that block — but before the existing `agent.output` emit — add:

```python
# Phase 3.5: enrich top papers with full text for Writer context.
# Skip when runs_dir wasn't injected (test fixtures) or there are no
# papers to enrich.
if findings.papers and self._runs_dir is not None:
    runs_papers_dir = (
        self._runs_dir / str(self.emitter.run_id) / "papers"
    )
    paths = await batch_enrich_papers(
        findings.papers,
        runs_papers_dir=runs_papers_dir,
        top_n=3,
        mailto=settings.polite_mailto or None,
    )
    # Phase 3.6: compact oversized files in place.
    paths = await self._compact_oversized_papers(runs_papers_dir, paths)
    findings = findings.model_copy(update={"paper_fulltext_paths": paths})
    await self.emitter.emit(
        "log",
        {
            "level": "info",
            "message": (
                f"enriched {len(paths)} of top 3 papers with full text"
            ),
        },
        agent=self.AGENT_NAME,
    )
```

- [ ] **Step 5: Wire `runs_dir` into pipeline.py at construction**

Edit `apps/agent-worker/src/agent_worker/pipeline.py`. Find the line constructing `SearcherAgent(...)` inside `run_pipeline`. Add `runs_dir=runs_dir` to its kwargs. There is already a `runs_dir = Path(settings.runs_dir).resolve()` line earlier in the function; reuse it.

```python
searcher = SearcherAgent(gateway, emitter, runs_dir=runs_dir, **kwargs)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/agent-worker && uv run pytest tests/test_searcher_agent.py --tb=short -q`
Expected: All existing tests pass + 2 new ones (8 → 10 in this file).

Run: `cd apps/agent-worker && uv run pytest tests/test_searcher_orchestration.py --tb=short -q`
Expected: All 12 orchestration tests still pass.

Run the full agent-worker suite:

```bash
cd apps/agent-worker && uv run pytest --tb=short -q
```

Expected: Around 312 passed (293 from Task 1 + 10 PDF tool + 4 compaction + 5 searcher extensions; some are double-counted). Investigate any new failures.

- [ ] **Step 7: Commit**

```bash
git add apps/agent-worker/src/agent_worker/agents/searcher.py apps/agent-worker/src/agent_worker/pipeline.py apps/agent-worker/tests/test_searcher_agent.py
git commit -m "feat(searcher): wire PDF enrichment + compaction into pipeline

Phase 3.5: batch_enrich_papers persists top 3 cited papers' full text to
runs/<id>/papers/NN.md. Phase 3.6: _compact_oversized_papers compresses
files > 24k chars in place. SearchFindings.paper_fulltext_paths carries
the persisted paths to Writer. SearcherAgent constructor gains runs_dir
(None in test fixtures => enrichment skipped, existing tests unaffected)."
```

---

## Task 7: Writer prompt template + integration

**Files:**
- Modify: `apps/agent-worker/src/agent_worker/prompts/writer/v1.toml` (add `paper_fulltexts` variable)
- Modify: `apps/agent-worker/src/agent_worker/agents/writer.py` (load paths, soft truncate, render)
- Modify: `apps/agent-worker/tests/test_writer_agent.py` (extend; if no such file, create)

- [ ] **Step 1: Decide test file location**

Run: `ls apps/agent-worker/tests/ | grep writer`

If `test_writer_agent.py` exists, you'll extend it. If not, you'll create a minimal one. The exact file name surfaces from `ls`; use it. Pick a writer test that already covers the `run_for` happy path and extend or mirror its style.

- [ ] **Step 2: Write the failing test**

Append (or create) a test that verifies the prompt body contains the full-text block when `paper_fulltext_paths` is non-empty. Example template — adapt the imports and helper fakes to whatever the existing test file uses:

```python
from pathlib import Path

from agent_worker.agents import WriterAgent
from mm_contracts import SearchFindings


async def test_writer_includes_paper_fulltexts_when_paths_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    # ... existing fixtures (problem, analysis, model_spec, coder_output) ...
) -> None:
    run_dir = tmp_path / "run"
    papers_dir = run_dir / "papers"
    papers_dir.mkdir(parents=True)
    (papers_dir / "01.md").write_text("paper 1 full text", encoding="utf-8")
    (papers_dir / "02.md").write_text("paper 2 full text", encoding="utf-8")

    captured_messages: list[dict] = []

    class _CapturingGateway:
        async def stream_completion(self, **kwargs):  # noqa: ANN003
            captured_messages.extend(kwargs.get("messages") or [])
            # Yield a minimally valid PaperDraft JSON so the test doesn't crash.
            yield (
                '{"title":"T","abstract":"a","sections":'
                '[{"title":"Intro","body_markdown":"x"}],'
                '"references":[],"figure_refs":[]}'
            )

        async def close(self) -> None:
            pass

    emitter = _FakeEmitter()
    findings = SearchFindings(
        queries=["q"],
        papers=[],  # Writer doesn't need filled papers for this assertion
        key_findings=[],
        datasets_mentioned=[],
        paper_fulltext_paths=["papers/01.md", "papers/02.md"],
    )
    agent = WriterAgent(_CapturingGateway(), emitter, run_dir=run_dir)  # type: ignore[arg-type]
    await agent.run_for(problem, analysis, model_spec, coder_output, findings)

    body = "\n".join(m.get("content", "") for m in captured_messages)
    assert "paper 1 full text" in body
    assert "paper 2 full text" in body
```

The exact WriterAgent constructor signature may differ — inspect `apps/agent-worker/src/agent_worker/agents/writer.py` to find how `run_dir` is currently surfaced (it may already be a constructor arg or it may need to be added).

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/agent-worker && uv run pytest tests/test_writer_agent.py -v -k paper_fulltexts`
Expected: FAIL — either `run_dir` kwarg unknown, or assert fails because Writer prompt does not include the new block.

- [ ] **Step 4: Add `paper_fulltexts` variable to the Writer prompt**

Edit `apps/agent-worker/src/agent_worker/prompts/writer/v1.toml`. Locate the user template (it's the section under `[user_template]` with `text = """..."""`). Just BEFORE the closing `"""`, insert a section:

```
## Supplementary full text for citation grounding

The following are the full text (or compacted summaries) of the top
papers retrieved by the Searcher. Use them to ground specific
citations — refer to equations, methodology details, and numerical
results verbatim where appropriate. When this block is empty, fall
back to title+abstract citation from the Sources list.

{{ paper_fulltexts }}
```

Keep the existing template variables (`problem_text`, `competition_type`, `analysis_json`, etc.) intact. Add `paper_fulltexts` as a new key wherever the template expects its render-arg map.

- [ ] **Step 5: Load paths and render in WriterAgent**

Edit `apps/agent-worker/src/agent_worker/agents/writer.py`. Add module-level constant near the top:

```python
WRITER_SOFT_TRUNCATE_CHARS = 32_000  # per-paper Writer-side budget
```

In the `run_for` method, just before constructing `messages = [...]` for the LLM call, build the supplementary block:

```python
fulltexts_block = ""
if findings.paper_fulltext_paths and self._run_dir is not None:
    blocks: list[str] = []
    for idx, rel_path in enumerate(findings.paper_fulltext_paths, start=1):
        abs_path = self._run_dir / rel_path
        if not abs_path.exists():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text) > WRITER_SOFT_TRUNCATE_CHARS:
            text = (
                text[:WRITER_SOFT_TRUNCATE_CHARS].rsplit("\n\n", 1)[0]
                + "\n\n[...truncated]"
            )
        blocks.append(
            f"### Paper {idx} (cite as findings.papers[{idx - 1}])\n\n{text}"
        )
    fulltexts_block = "\n\n".join(blocks)
```

Pass `paper_fulltexts=fulltexts_block` into `self.prompt.render_user(...)` alongside the existing template variables.

If WriterAgent does not yet take `run_dir`, add it:

```python
class WriterAgent:
    def __init__(
        self,
        gateway: GatewayClient,
        emitter: EventEmitter,
        # ... existing args ...
        run_dir: Path | None = None,
    ) -> None:
        # ... existing assignments ...
        self._run_dir = run_dir
```

- [ ] **Step 6: Wire `run_dir` into pipeline.py for WriterAgent**

Edit `apps/agent-worker/src/agent_worker/pipeline.py`. Find the `WriterAgent(...)` construction. The pipeline already has `run_dir = runs_dir / str(run_id)` available (used for paper.md / paper.meta.json writes). Pass it:

```python
writer = WriterAgent(gateway, emitter, run_dir=run_dir, **kwargs)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/agent-worker && uv run pytest tests/test_writer_agent.py --tb=short -q`
Expected: All existing writer tests pass + new one passes.

Then the full suite:

```bash
cd apps/agent-worker && uv run pytest --tb=short -q
```

Expected: All pass.

Run lint:

```bash
cd /Users/cornna/project/math_agent && uvx ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add apps/agent-worker/src/agent_worker/agents/writer.py apps/agent-worker/src/agent_worker/pipeline.py apps/agent-worker/src/agent_worker/prompts/writer/v1.toml apps/agent-worker/tests/test_writer_agent.py
git commit -m "feat(writer): inline paper full text from SearchFindings.paper_fulltext_paths

Writer reads runs/<id>/papers/NN.md (raw or compacted) and inlines into
prompt with 32k-char soft truncate at paragraph boundary as the last
safety net. New paper_fulltexts template variable in writer/v1.toml.
Falls back cleanly to abstract-only citation when paths are empty."
```

---

## Task 8: End-to-end local verification

**Files:** None (manual verification step)

- [ ] **Step 1: Push the branch + open PR**

```bash
git push -u origin <current-branch>
gh pr create --base main --title "feat(searcher): PDF enrichment with compaction (top 3 papers)" --body "Implements docs/superpowers/specs/2026-05-11-searcher-pdf-enrichment-design.md. Top 3 cited papers in SearchFindings get their full text fetched (arXiv direct + Unpaywall for DOIs), extracted via pdfplumber/trafilatura, compacted via an inline LLM call when > 24k chars, and inlined into Writer's prompt for grounded citations."
```

- [ ] **Step 2: Wait for CI**

```bash
gh pr checks <PR-NUM> --watch
```

Expected: All checks pass (python ruff+pytest, rust, web typecheck, contracts-drift). If anything fails, fix root cause and push.

- [ ] **Step 3: Squash merge**

After CI green and any review comments addressed:

```bash
gh pr merge <PR-NUM> --squash --delete-branch
```

- [ ] **Step 4: Optional — rebuild local stack and verify on a real run**

If you want to verify end-to-end before tagging a release:

```bash
docker compose -f docker-compose.prod.yml build worker
docker compose -f docker-compose.prod.yml up -d
curl -sS -X POST http://127.0.0.1:8080/runs \
  -H "Authorization: Bearer dev-local-insecure-token" \
  -H "Content-Type: application/json" \
  -d '{"problem_text": "请用排队论分析一个有 2 个服务台的银行网点高峰期等候时间，给出 M/M/c 模型与建议", "competition_type": "cumcm", "reasoning_effort": "low", "model_override": "gpt-5.4"}'
```

Then wait for completion and inspect `runs/<id>/papers/` to confirm 1-3 `.md` files were written.

---

## Self-Review

**Spec coverage:**

| Spec section | Task that implements |
|---|---|
| `tools/pdf.py::find_pdf_url` | Task 2 |
| `tools/pdf.py::fetch_and_extract` | Task 3 |
| `tools/pdf.py::batch_enrich_papers` | Task 4 |
| `SearchFindings.paper_fulltext_paths` contract | Task 1 |
| trafilatura + pdfplumber deps | Task 1 |
| TS mirror | Task 1 |
| SearcherAgent constructor `runs_dir` | Task 6 |
| SearcherAgent.run_for Phase 3.5 wiring | Task 6 |
| SearcherAgent compaction method + constants | Task 5 |
| Compaction prompt (preserve language) | Task 5 |
| WriterAgent prompt `paper_fulltexts` | Task 7 |
| WriterAgent file loading + 32k soft truncate | Task 7 |
| pipeline.py SearcherAgent runs_dir injection | Task 6 |
| pipeline.py WriterAgent run_dir injection | Task 7 |
| Failure mode coverage in tests | Tasks 2, 3, 4, 5, 6 (each test class covers its own failure modes) |
| End-to-end local verification | Task 8 |

All spec sections accounted for.

**Placeholder scan:** No "TBD", "TODO", "fill in", or "appropriate error handling". Every code step contains the full code. Test file paths are exact. The one acknowledged unknown — whether `WriterAgent.__init__` already takes `run_dir` — is paired with explicit "inspect first, then add only if missing" guidance.

**Type consistency:**
- `paper_fulltext_paths: list[str]` (Pydantic, max_length=3) consistent across Task 1, 6, 7
- `find_pdf_url(paper, *, mailto=None) -> str | None` consistent in Task 2, 4
- `fetch_and_extract(url, *, timeout=15, max_bytes=20_000_000) -> tuple[str | None, Literal[...]]` consistent in Task 3, 4
- `batch_enrich_papers(papers, runs_papers_dir, top_n=3, *, mailto=None, concurrency=3) -> list[str]` consistent in Task 4, 6
- `_compact_oversized_papers(self, runs_papers_dir, paths) -> list[str]` consistent in Task 5, 6
- `_compact_one(self, raw_text) -> str | None` consistent in Task 5
- `COMPACT_THRESHOLD_CHARS = 24_000`, `COMPACT_TARGET_CHARS = 24_000`, `COMPACT_MIN_OUTPUT_CHARS = 1_000`, `WRITER_SOFT_TRUNCATE_CHARS = 32_000` consistent throughout
