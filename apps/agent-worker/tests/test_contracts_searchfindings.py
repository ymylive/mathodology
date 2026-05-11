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
