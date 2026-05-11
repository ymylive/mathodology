# Searcher PDF Enrichment

**Date:** 2026-05-11
**Status:** Approved (brainstorming)
**Author:** brainstorming session with project owner

## Problem

Today the Searcher emits a `SearchFindings` whose `papers[]` carries only
title + abstract per paper (the metadata available from arXiv / OpenAlex /
Crossref retrieval). Downstream the Writer cites these papers but cannot
ground specific claims in section-level content — every reference reads
"according to [12]" without distinguishing whether [12] supports a
specific formula, an experimental setup detail, or a numerical result.

v0.5.2 end-to-end verification on a Chinese M/M/c bank-queueing problem
illustrated this gap: 8 retained papers (Indonesia/Bangladesh/Nigeria bank
case studies, classic Erlang C work) — Writer could only reference them
generically because their full text was never fetched.

## Goal

Enrich `SearchFindings` with full-text content for the top 3 papers so the
Writer can cite specific equations, methodology details, and experimental
numbers. Fail gracefully when full text is unavailable.

## Non-goals

- Tavily / open-webSearch web hits are **not** treated as papers — no PDF
  extraction for non-academic URLs.
- Modeler does **not** consume full text — stage stays focused on HMML and
  the Analyzer output.
- No cross-run FTS5 cache (separate optimization candidate, out of scope).
- No paywalled content scraping. Unpaywall-only OA discovery for DOIs.

## Architecture

```
SearchFindings produced (existing path)
        │
        ▼
[NEW] Phase 3.5: batch_enrich_papers(findings.papers[:3])
        │
        │  per paper (concurrency=3):
        │    ├─ find_pdf_url()
        │    │    ├─ arXiv ID present → https://arxiv.org/pdf/<id>.pdf
        │    │    └─ DOI present       → Unpaywall API → best_oa_location.url_for_pdf
        │    │                          (None when no OA version exists)
        │    └─ fetch_and_extract()
        │         ├─ Content-Type contains "pdf"  → pdfplumber
        │         └─ Content-Type contains "html" → trafilatura
        ▼
runs/<run_id>/papers/01.md, 02.md, 03.md  (raw extracted text;
                                            failed papers are not written)
        │
        ▼
[NEW] Phase 3.6: _compact_oversized_papers()
        │
        │  per saved file:
        │    └─ if len(text) > 24000 chars:
        │         └─ one LLM call → compacted markdown → overwrite file
        ▼
SearchFindings.paper_fulltext_paths = ["papers/01.md", "papers/02.md", ...]
        │
        ▼
Writer reads paths, inlines content into prompt with 32k-char paragraph-
  boundary soft truncation as last-resort safety net
```

## Why these choices

| Decision | Rationale |
|---|---|
| Top 3 papers | Caps Writer context bloat (~45-90k tokens worst case) while keeping ≥3 citable sources for an MCM-grade paper |
| Writer-only consumption | Modeler/Coder paths stay focused on their own concerns; one consumer = simpler debugging |
| arXiv direct + Unpaywall for DOIs | arXiv is always-open; Unpaywall covers the long tail of OA versions for Crossref/OpenAlex sources without paywall scraping |
| Paths in contract, content on disk | SearchFindings stays small; historical runs can re-read; matches existing `notebook_path` / `paper_path` convention |
| Compaction over truncation | Truncation can cut the methodology section that has the equation we wanted to cite; compaction preserves citable density |
| Compaction as Searcher method, not new Agent | Same pattern as `_refine_queries` — internal LLM utility, not a Critic-evaluable stage. Avoids stage.start/done noise and per-paper Critic budget. |

## Interfaces

### New tool: `apps/agent-worker/src/agent_worker/tools/pdf.py`

```python
class PdfFetchResult(BaseModel):
    paper_url: str
    pdf_url: str | None
    text: str | None
    bytes_fetched: int = 0
    parser: Literal["trafilatura", "pdfplumber", "none"] = "none"
    error: str | None = None


async def find_pdf_url(
    paper: Paper, *, mailto: str | None = None
) -> str | None:
    """Resolve a Paper to a fetchable PDF URL.

    Resolution order:
      1. arXiv ID present → derive `https://arxiv.org/pdf/<id>.pdf`
      2. DOI present → Unpaywall API
         (https://api.unpaywall.org/v2/<doi>?email=<mailto>) →
         `best_oa_location.url_for_pdf`
      3. Otherwise None
    """


async def fetch_and_extract(
    url: str, *, timeout: float = 15.0, max_bytes: int = 20_000_000
) -> tuple[str | None, str]:
    """Fetch URL and extract plain text.

    Returns (text, parser_name). text=None signals failure. 20MB ceiling
    rejects oversized PDFs to bound memory.
    """


async def batch_enrich_papers(
    papers: list[Paper],
    runs_papers_dir: Path,
    top_n: int = 3,
    *,
    mailto: str | None = None,
    concurrency: int = 3,
) -> list[str]:
    """Enrich the top N papers concurrently and persist successes.

    Returns a list of relative paths under runs/<run_id>/, e.g.
    ['papers/01.md', 'papers/03.md']. Failed papers do not appear.
    Index in the filename matches the original `papers[]` index.
    """
```

### Contract change

`packages/py-contracts/src/mm_contracts/agent_io.py`:

```python
class SearchFindings(BaseModel):
    # ...existing fields unchanged
    paper_fulltext_paths: list[str] = Field(
        default_factory=list, max_length=3
    )
```

`packages/ts-contracts/src/index.ts`:

```ts
export interface SearchFindings {
  // ...existing
  paper_fulltext_paths?: string[];
}
```

Backward compatible: default empty list, optional in TS.

### Searcher integration

`apps/agent-worker/src/agent_worker/agents/searcher.py` constructor gains
`runs_dir: Path` (pipeline.py already has `settings.runs_dir` at hand).

`SearcherAgent.run_for()` after `findings = await self._synthesize(...)`:

```python
# Phase 3.5: enrich top papers with full text for Writer.
if findings.papers:
    runs_papers_dir = self._runs_dir / str(self.emitter.run_id) / "papers"
    runs_papers_dir.mkdir(parents=True, exist_ok=True)
    paths = await batch_enrich_papers(
        findings.papers,
        runs_papers_dir=runs_papers_dir,
        top_n=3,
        mailto=settings.polite_mailto or None,
    )
    # Phase 3.6: compact oversized papers in place.
    paths = await self._compact_oversized_papers(runs_papers_dir, paths)
    findings = findings.model_copy(update={"paper_fulltext_paths": paths})
    await self.emitter.emit(
        "log",
        {
            "level": "info",
            "message": f"enriched {len(paths)}/3 top papers with PDF text",
        },
        agent=self.AGENT_NAME,
    )
```

### Compaction

Constants on SearcherAgent:

```python
COMPACT_THRESHOLD_CHARS = 24_000   # ~6k tokens; raw files above this compact
COMPACT_TARGET_CHARS    = 24_000   # output ceiling
COMPACT_MIN_OUTPUT_CHARS = 1_000   # below this, treat compaction as failed
WRITER_SOFT_TRUNCATE_CHARS = 32_000  # Writer-side last-resort safety
```

Method:

```python
async def _compact_oversized_papers(
    self, runs_papers_dir: Path, paths: list[str]
) -> list[str]:
    """Compact any file whose raw text > THRESHOLD. Overwrite in place.

    On LLM failure or short output, leaves the raw file untouched —
    the Writer side has its own char-budget soft truncation.
    """
```

Compaction prompt (inline, no TOML):

```
SYSTEM: You compress an academic paper into a high-density summary for
downstream LLM citation. Preserve verbatim:
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
Respond with the compacted markdown only.

USER: <raw extracted text>
```

### Writer integration

`apps/agent-worker/src/agent_worker/agents/writer.py` reads paths before
LLM call, applies Writer-side soft truncation, and injects via prompt
template variable `{{ paper_fulltexts }}`:

```python
fulltexts_block = ""
if findings.paper_fulltext_paths:
    blocks = []
    for idx, rel_path in enumerate(findings.paper_fulltext_paths, start=1):
        abs_path = run_dir / rel_path
        if not abs_path.exists():
            continue
        text = abs_path.read_text(encoding="utf-8")
        if len(text) > WRITER_SOFT_TRUNCATE_CHARS:
            text = (
                text[:WRITER_SOFT_TRUNCATE_CHARS].rsplit("\n\n", 1)[0]
                + "\n\n[...truncated]"
            )
        blocks.append(
            f"### Paper {idx} (cite as findings.papers[{idx-1}])\n\n{text}"
        )
    fulltexts_block = "\n\n".join(blocks)
```

The Writer prompt TOML (`prompts/writer/v1.toml`) gains a `paper_fulltexts`
template variable in its user template, e.g.:

```
## Supplementary full text for citation grounding

{{ paper_fulltexts }}

(If empty, fall back to title+abstract citation from findings.papers.)
```

## Failure modes

| Failure | Behavior | User-visible effect |
|---|---|---|
| Paper has neither arxiv_id nor DOI | `find_pdf_url` returns None | That slot empty |
| Unpaywall returns no OA version for DOI | `find_pdf_url` returns None | That slot empty |
| PDF download HTTP 5xx / timeout | `fetch_and_extract` returns (None, "none") | That slot empty |
| PDF size > 20MB | Rejected before parse | That slot empty |
| pdfplumber raises | Caught, returns (None, "none") | That slot empty |
| trafilatura returns empty | Treated as failure | That slot empty |
| All 3 enrichment fail | `paper_fulltext_paths=[]` | Writer falls back to abstract-only (current v0.5.3 behavior) |
| `SearchFindings.papers` already empty | Skip enrich entirely | No change |
| Compaction LLM call fails | Keep raw file | Writer-side soft truncate kicks in |
| Compaction returns < 1000 chars | Treat as failure, keep raw | Same as above |
| Compaction output still > 32k | Accept it | Writer-side soft truncate kicks in |

## Cost & latency

| Item | Worst-case |
|---|---|
| Network fetches | 3 papers × (1 Unpaywall + 1 PDF download) = 6 HTTP requests, ~5s wall time at concurrency=3 |
| pdfplumber CPU | 3 papers × 5-10s = 15-30s; can run in process pool to parallelize, but 5-10s sync is acceptable |
| Compaction LLM | Up to 3 × ~$0.005 = ~$0.015 added per run; only triggers when raw > 24k chars |
| Writer context | +45-90k tokens to Writer's user prompt (3 papers × 15-30k chars each) |
| Image size | +30-50MB (trafilatura + pdfplumber + transitive deps) |

## Dependencies

`apps/agent-worker/pyproject.toml`:

```toml
trafilatura = "^1.12"
pdfplumber = "^0.11"
```

Both pure Python; no system libraries, no Docker change.

## Tests

| File | New/Modified | Coverage |
|---|---|---|
| `tests/test_pdf_tool.py` | new (8 tests) | find_pdf_url (3 paths: arxiv / DOI→OA / DOI→no OA); fetch_and_extract (PDF + HTML + timeout + 20MB ceiling); batch_enrich_papers (concurrency cap + failure isolation) |
| `tests/test_searcher_compaction.py` | new (4 tests) | Compaction triggers / does not trigger; LLM failure keeps raw; empty response keeps raw |
| `tests/test_searcher_agent.py` | extended (+2 tests) | `paper_fulltext_paths` is populated; empty list when all enrichment fails |
| `tests/test_writer_agent.py` | extended (+1 test) | Writer prompt includes full text block when paths non-empty |

All HTTP/PDF use `httpx_mock`. Unpaywall API mocked at fixture level.
pdfplumber tested against a minimal real-PDF byte stream stored under
`tests/fixtures/sample-paper.pdf` (5KB).

Target: 290 (current main) + 15 new = ~305 tests passed.

## Implementation order

1. **Dependencies + contract**
   - `uv add trafilatura pdfplumber` in agent-worker
   - Extend `Paper` doc / `SearchFindings` with `paper_fulltext_paths`
   - TS mirror in `packages/ts-contracts/src/index.ts`
2. **Tool layer**
   - `tools/pdf.py` with `find_pdf_url`, `fetch_and_extract`, `batch_enrich_papers`
   - `tests/test_pdf_tool.py`
3. **Searcher integration**
   - Inject `runs_dir` constructor arg (also touch pipeline.py construction site)
   - Add `_compact_oversized_papers` + `_compact_one` methods + constants
   - Wire Phase 3.5 + 3.6 into `run_for`
   - `tests/test_searcher_compaction.py` + extensions to `test_searcher_agent.py`
4. **Writer integration**
   - Prompt TOML gains `paper_fulltexts` variable
   - `run_for` loads paths, applies soft truncate, renders
   - Extension to `tests/test_writer_agent.py`
5. **End-to-end**
   - Single PR with all of the above
   - CI green → local stack rebuild → repeat v0.5.2 Chinese M/M/c run →
     verify `runs/<id>/papers/01-03.md` exist and Writer cites specific
     equations / parameter values

## Open questions resolved during brainstorming

- Compaction language: preserve source language (no translation).
- Compaction is an internal Searcher method, not a separate Agent class
  (no Critic evaluation, no stage events).
- Out-of-band file storage with path-in-contract over inline content in
  contract.
- One PR over multi-PR (logic is tightly coupled).
