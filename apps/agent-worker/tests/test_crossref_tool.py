"""Tests for the Crossref tool — offline via pytest-httpx."""

from __future__ import annotations

import httpx
import pytest
from agent_worker.tools.crossref import (
    CROSSREF_API_URL,
    _format_issued_date,
    _parse_works,
    _strip_jats_tags,
    batch_search_crossref,
    search_crossref,
)

_SAMPLE_RESPONSE = {
    "message": {
        "items": [
            {
                "DOI": "10.1234/abc.2024.5678",
                "title": ["A Survey of Adaptive Traffic Control"],
                "author": [
                    {"family": "Smith", "given": "Xiao"},
                    {"family": "Lee", "given": "Yun"},
                ],
                "abstract": "<jats:p>We review modern <jats:italic>RL</jats:italic> methods for traffic signals.</jats:p>",
                "issued": {"date-parts": [[2024, 5, 12]]},
                "URL": "https://doi.org/10.1234/abc.2024.5678",
            },
            {
                # Year-only date; no abstract; no DOI prefix on URL.
                "DOI": "10.5555/year-only.2023",
                "title": ["Year-Only Paper"],
                "author": [{"name": "Quanta Corp"}],  # non-personal author
                "issued": {"date-parts": [[2023]]},
                "URL": "https://example.org/abs/year-only.2023",
            },
            {
                # No DOI, fall back to URL.
                "title": ["Pre-print on Server X"],
                "URL": "https://preprints.example/abs/2024/01",
                "issued": {"date-parts": [[]]},
            },
        ]
    }
}


def test_parse_works_extracts_fields() -> None:
    papers = _parse_works(_SAMPLE_RESPONSE)
    assert len(papers) == 3

    p0 = papers[0]
    assert p0.title == "A Survey of Adaptive Traffic Control"
    assert p0.authors == ["Xiao Smith", "Yun Lee"]
    assert p0.doi == "10.1234/abc.2024.5678"
    assert p0.url == "https://doi.org/10.1234/abc.2024.5678"
    # JATS tags stripped, prose preserved.
    assert "RL" in p0.abstract
    assert "<jats" not in p0.abstract
    assert p0.published == "2024-05-12"

    p1 = papers[1]
    assert p1.published == "2023"
    assert p1.authors == ["Quanta Corp"]
    assert p1.abstract == ""

    p2 = papers[2]
    assert p2.doi is None
    assert p2.url == "https://preprints.example/abs/2024/01"
    assert p2.published is None  # empty date-parts → None


def test_parse_works_skips_titleless_items() -> None:
    papers = _parse_works(
        {
            "message": {
                "items": [
                    {"DOI": "10.x/empty"},  # no title
                    {"DOI": "10.x/ok", "title": ["Real Title"]},
                ]
            }
        }
    )
    assert len(papers) == 1
    assert papers[0].title == "Real Title"


def test_parse_works_drops_items_without_doi_or_url() -> None:
    """Without DOI AND without URL, we have no identifier — drop the item."""
    papers = _parse_works(
        {"message": {"items": [{"title": ["Floating Title"]}]}}
    )
    assert papers == []


def test_parse_works_empty_or_malformed() -> None:
    assert _parse_works({}) == []
    assert _parse_works({"message": None}) == []
    assert _parse_works({"message": {"items": None}}) == []
    assert _parse_works({"message": {"items": []}}) == []


def test_strip_jats_tags_basic() -> None:
    out = _strip_jats_tags(
        "<jats:p>Hello <jats:bold>world</jats:bold> from JATS.</jats:p>"
    )
    assert "Hello" in out
    assert "world" in out
    assert "<" not in out and ">" not in out


def test_format_issued_date_variants() -> None:
    assert _format_issued_date({"date-parts": [[2024, 3, 15]]}) == "2024-03-15"
    assert _format_issued_date({"date-parts": [[2024, 3]]}) == "2024-03"
    assert _format_issued_date({"date-parts": [[2024]]}) == "2024"
    assert _format_issued_date({"date-parts": [[]]}) is None
    assert _format_issued_date({}) is None
    assert _format_issued_date(None) is None
    # Non-numeric parts → None, not a crash.
    assert _format_issued_date({"date-parts": [[None, "x"]]}) is None


async def test_search_crossref_sends_user_agent(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(json={"message": {"items": []}})
    await search_crossref("anything", mailto="bot@example.com")
    req = httpx_mock.get_request()
    ua = req.headers.get("user-agent") or req.headers.get("User-Agent") or ""
    assert "mailto:bot@example.com" in ua


async def test_search_crossref_mocked(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(json=_SAMPLE_RESPONSE)
    papers = await search_crossref("traffic", max_results=5)
    assert len(papers) == 3
    assert papers[0].doi == "10.1234/abc.2024.5678"


async def test_search_crossref_swallows_owned_client_errors(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(status_code=502)
    with pytest.raises(httpx.HTTPStatusError):
        await search_crossref("anything")


async def test_batch_search_crossref_skips_failed_queries(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(json=_SAMPLE_RESPONSE)
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json={"message": {"items": []}})

    results = await batch_search_crossref(["q1", "q2", "q3"], max_per_query=5)
    assert set(results.keys()) == {"q1", "q2", "q3"}
    # q1 returns 3, q2 429s → 0, q3 empty → 0.
    total = sum(len(v) for v in results.values())
    assert total == 3


async def test_batch_search_crossref_empty_queries() -> None:
    assert await batch_search_crossref([]) == {}


def test_endpoint_constant() -> None:
    """A regression guard: the endpoint constant is exposed for callers."""
    assert CROSSREF_API_URL.startswith("https://api.crossref.org/")
