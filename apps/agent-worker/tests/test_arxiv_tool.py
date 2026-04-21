"""Tests for the arXiv tool — all offline, via pytest-httpx mocking."""

from __future__ import annotations

import pytest
from agent_worker.tools.arxiv import (
    ARXIV_API_URL,
    _parse_atom,
    batch_search_arxiv,
    search_arxiv,
)

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2312.01234v2</id>
    <published>2023-12-01T00:00:00Z</published>
    <title>A Study of Ordinary Least Squares</title>
    <summary>
      We present a comprehensive study of OLS regression on large datasets.
    </summary>
    <author><name>Alice A.</name></author>
    <author><name>Bob B.</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00001</id>
    <published>2024-01-15T12:00:00Z</published>
    <title>Queueing Theory Revisited</title>
    <summary>M/M/1 analysis redux.</summary>
    <author><name>Carol C.</name></author>
  </entry>
</feed>
"""

_EMPTY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
</feed>
"""


def test_parse_atom_extracts_fields() -> None:
    papers = _parse_atom(_SAMPLE_ATOM)
    assert len(papers) == 2

    p0 = papers[0]
    assert p0.title == "A Study of Ordinary Least Squares"
    assert p0.authors == ["Alice A.", "Bob B."]
    assert "OLS regression" in p0.abstract
    assert p0.url == "http://arxiv.org/abs/2312.01234v2"
    # Version suffix stripped.
    assert p0.arxiv_id == "2312.01234"
    assert p0.published == "2023-12-01T00:00:00Z"

    p1 = papers[1]
    assert p1.arxiv_id == "2401.00001"  # no version suffix, unchanged
    assert p1.authors == ["Carol C."]


def test_parse_atom_empty_feed_returns_empty_list() -> None:
    assert _parse_atom(_EMPTY_ATOM) == []


def test_parse_atom_malformed_returns_empty_list() -> None:
    assert _parse_atom("<not-xml") == []
    assert _parse_atom("") == []


async def test_search_arxiv_mocked(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=(
            f"{ARXIV_API_URL}?search_query=all%3Aordinary%20least%20squares"
            "&start=0&max_results=5&sortBy=relevance&sortOrder=descending"
        ),
        text=_SAMPLE_ATOM,
    )
    papers = await search_arxiv("ordinary least squares", max_results=5)
    assert len(papers) == 2
    assert papers[0].title.startswith("A Study of Ordinary Least Squares")


async def test_search_arxiv_empty(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(text=_EMPTY_ATOM)
    assert await search_arxiv("no results here") == []


async def test_search_arxiv_malformed(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(text="<this is not a valid atom feed")
    # Should return [] without raising.
    assert await search_arxiv("anything") == []


async def test_batch_search_arxiv(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    # Return the same atom payload for every request (pytest-httpx matches
    # on the underlying URL sans query matching when we don't pin it).
    httpx_mock.add_response(text=_SAMPLE_ATOM)
    httpx_mock.add_response(text=_SAMPLE_ATOM)
    httpx_mock.add_response(text=_EMPTY_ATOM)

    results = await batch_search_arxiv(["q1", "q2", "q3"], max_per_query=5, concurrency=2)
    assert set(results.keys()) == {"q1", "q2", "q3"}
    # Two queries returned papers, one empty.
    total = sum(len(v) for v in results.values())
    assert total == 4


async def test_batch_search_arxiv_empty_queries() -> None:
    assert await batch_search_arxiv([]) == {}


@pytest.mark.parametrize(
    "xml_payload,expected_count",
    [
        (_SAMPLE_ATOM, 2),
        (_EMPTY_ATOM, 0),
    ],
)
def test_parse_atom_parametrized(xml_payload: str, expected_count: int) -> None:
    assert len(_parse_atom(xml_payload)) == expected_count
