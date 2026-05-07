"""Tests for the OpenAlex tool — offline via pytest-httpx."""

from __future__ import annotations

import httpx
import pytest
from agent_worker.tools.openalex import (
    OPENALEX_API_URL,
    _parse_works,
    _reconstruct_inverted_abstract,
    batch_search_openalex,
    search_openalex,
)

_SAMPLE_WORKS = {
    "results": [
        {
            "id": "https://openalex.org/W123456789",
            "doi": "https://doi.org/10.1234/example.2024.001",
            "title": "Adaptive Signal Timing via Reinforcement Learning",
            "publication_date": "2024-03-15",
            "authorships": [
                {"author": {"display_name": "Alice A."}},
                {"author": {"display_name": "Bob B."}},
            ],
            "abstract_inverted_index": {
                "We": [0],
                "propose": [1],
                "an": [2],
                "RL-based": [3],
                "controller.": [4],
            },
        },
        {
            "id": "https://openalex.org/W987654321",
            # No DOI — exercise the openalex.id fallback path.
            "doi": None,
            "title": "Queueing Theory Revisited",
            "publication_date": "2023-09-01",
            "authorships": [{"author": {"display_name": "Carol C."}}],
            "abstract_inverted_index": None,
        },
    ]
}


def test_parse_works_extracts_fields() -> None:
    papers = _parse_works(_SAMPLE_WORKS)
    assert len(papers) == 2

    p0 = papers[0]
    assert p0.title.startswith("Adaptive Signal Timing")
    assert p0.authors == ["Alice A.", "Bob B."]
    assert p0.doi == "10.1234/example.2024.001"
    # DOI is preferred over the openalex.id when present.
    assert p0.url == "https://doi.org/10.1234/example.2024.001"
    assert p0.published == "2024-03-15"
    # Inverted index reconstructed in position order.
    assert p0.abstract == "We propose an RL-based controller."

    p1 = papers[1]
    assert p1.doi is None
    # Falls back to the openalex.id when DOI missing.
    assert p1.url == "https://openalex.org/W987654321"
    assert p1.abstract == ""  # missing index → empty


def test_parse_works_strips_doi_url_prefix() -> None:
    papers = _parse_works(
        {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "doi": "https://doi.org/10.1/abc",
                    "title": "T",
                }
            ]
        }
    )
    assert papers[0].doi == "10.1/abc"


def test_parse_works_skips_titleless_entries() -> None:
    """Entries without a title are dropped, not raised on."""
    papers = _parse_works(
        {
            "results": [
                {"id": "https://openalex.org/W1", "title": ""},
                {"id": "https://openalex.org/W2", "title": "Real Title"},
            ]
        }
    )
    assert len(papers) == 1
    assert papers[0].title == "Real Title"


def test_parse_works_empty_or_malformed() -> None:
    assert _parse_works({}) == []
    assert _parse_works({"results": None}) == []
    assert _parse_works({"results": []}) == []


def test_reconstruct_inverted_abstract_handles_missing_index() -> None:
    assert _reconstruct_inverted_abstract(None) == ""
    assert _reconstruct_inverted_abstract({}) == ""
    # Non-int positions are ignored.
    assert _reconstruct_inverted_abstract({"x": ["bad"]}) == ""


def test_reconstruct_inverted_abstract_orders_by_position() -> None:
    idx = {"second": [1], "first": [0], "third": [2]}
    assert _reconstruct_inverted_abstract(idx) == "first second third"


async def test_search_openalex_mocked(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=(
            f"{OPENALEX_API_URL}?search=signal%20timing&per-page=5"
            "&select=id%2Cdoi%2Ctitle%2Cpublication_date%2Cauthorships%2Cabstract_inverted_index"
        ),
        json=_SAMPLE_WORKS,
    )
    papers = await search_openalex("signal timing", max_results=5)
    assert len(papers) == 2
    assert papers[0].doi == "10.1234/example.2024.001"


async def test_search_openalex_passes_mailto(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """When mailto is supplied it must be forwarded as a query parameter."""
    httpx_mock.add_response(json={"results": []})
    await search_openalex("anything", mailto="bot@example.com")
    request = httpx_mock.get_request()
    assert "mailto=bot%40example.com" in str(request.url)


async def test_search_openalex_swallows_owned_client_errors(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """When we own the client and the server returns 5xx, propagate raises."""
    httpx_mock.add_response(status_code=503)
    with pytest.raises(httpx.HTTPStatusError):
        await search_openalex("anything")


async def test_batch_search_openalex_skips_failed_queries(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(json=_SAMPLE_WORKS)
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json={"results": []})

    results = await batch_search_openalex(["q1", "q2", "q3"], max_per_query=5)
    assert set(results.keys()) == {"q1", "q2", "q3"}
    # q1 succeeds (2), q2 429s (0 — wrapped), q3 empty (0).
    total = sum(len(v) for v in results.values())
    assert total == 2


async def test_batch_search_openalex_empty_queries() -> None:
    assert await batch_search_openalex([]) == {}
