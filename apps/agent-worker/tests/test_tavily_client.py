"""Tests for the Tavily search tool — all offline, via pytest-httpx mocking.

Exercises the response-parsing happy path plus the three failure modes the
Searcher relies on: auth failure (401/403), rate-limit (429), and per-query
timeout. None of these should raise; they must all yield empty lists so the
orchestrator can fall back to open-webSearch without special-casing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson
import pytest
from agent_worker.tools.tavily import (
    TAVILY_API_URL,
    TavilyResult,
    _parse_results,
    batch_search_tavily,
    search_tavily,
)


_SAMPLE_PAYLOAD: dict[str, Any] = {
    "query": "ordinary least squares",
    "results": [
        {
            "title": "OLS explained",
            "url": "https://example.com/ols",
            "content": "Ordinary least squares minimizes the sum of squared residuals.",
            "score": 0.92,
            "published_date": "2024-01-15",
        },
        {
            "title": "Linear Regression in Python",
            "url": "https://example.com/lr",
            "content": "A walkthrough of sklearn LinearRegression.",
            "score": 0.81,
            "published_date": None,
        },
    ],
}


def test_parse_results_extracts_fields() -> None:
    out = _parse_results(_SAMPLE_PAYLOAD, query="ordinary least squares")
    assert len(out) == 2
    r0 = out[0]
    assert r0.title == "OLS explained"
    assert r0.url == "https://example.com/ols"
    assert r0.score == pytest.approx(0.92)
    assert r0.published_date == "2024-01-15"
    assert r0.query == "ordinary least squares"
    # null published_date → None, not the string "None".
    assert out[1].published_date is None


def test_parse_results_drops_missing_title_or_url() -> None:
    payload = {
        "results": [
            {"title": "", "url": "https://a"},  # no title
            {"title": "t", "url": ""},  # no url
            {"title": "ok", "url": "https://b", "content": "x", "score": 0.5},
        ]
    }
    out = _parse_results(payload, query="q")
    assert len(out) == 1
    assert out[0].url == "https://b"


def test_parse_results_garbage_returns_empty() -> None:
    assert _parse_results({}, query="q") == []
    assert _parse_results({"results": "not a list"}, query="q") == []
    assert _parse_results("not a dict", query="q") == []
    assert _parse_results({"results": [42, None]}, query="q") == []


def test_parse_results_coerces_non_string_score() -> None:
    payload = {
        "results": [
            {"title": "t", "url": "https://x", "score": "not-a-number"},
        ]
    }
    out = _parse_results(payload, query="q")
    assert len(out) == 1
    assert out[0].score == 0.0  # degraded, not raised


async def test_search_tavily_happy_path(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=TAVILY_API_URL,
        method="POST",
        json=_SAMPLE_PAYLOAD,
    )
    got = await search_tavily("ordinary least squares", api_key="test-key")
    assert len(got) == 2
    assert got[0].url == "https://example.com/ols"
    assert all(isinstance(r, TavilyResult) for r in got)


async def test_search_tavily_401_returns_empty(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=TAVILY_API_URL,
        method="POST",
        status_code=401,
        json={"error": "Invalid API key"},
    )
    assert await search_tavily("q", api_key="bad") == []


async def test_search_tavily_403_returns_empty(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=TAVILY_API_URL,
        method="POST",
        status_code=403,
        json={"error": "Forbidden"},
    )
    assert await search_tavily("q", api_key="x") == []


async def test_search_tavily_429_returns_empty(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=TAVILY_API_URL,
        method="POST",
        status_code=429,
        json={"error": "rate limited"},
    )
    assert await search_tavily("q", api_key="x") == []


async def test_search_tavily_500_returns_empty(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=TAVILY_API_URL,
        method="POST",
        status_code=500,
        text="server boom",
    )
    assert await search_tavily("q", api_key="x") == []


async def test_search_tavily_non_json_returns_empty(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        url=TAVILY_API_URL,
        method="POST",
        status_code=200,
        text="this is not JSON",
    )
    assert await search_tavily("q", api_key="x") == []


async def test_batch_search_tavily_happy_path(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    # pytest-httpx matches responses in FIFO order across concurrent POSTs,
    # so mirror two identical payloads for two queries.
    for _ in range(2):
        httpx_mock.add_response(
            url=TAVILY_API_URL, method="POST", json=_SAMPLE_PAYLOAD
        )
    got = await batch_search_tavily(
        ["q1", "q2"], api_key="test-key", concurrency=2
    )
    assert set(got.keys()) == {"q1", "q2"}
    assert sum(len(v) for v in got.values()) == 4


async def test_batch_search_tavily_no_api_key_short_circuits() -> None:
    got = await batch_search_tavily(["q1", "q2"], api_key="")
    assert got == {"q1": [], "q2": []}


async def test_batch_search_tavily_empty_queries() -> None:
    assert await batch_search_tavily([], api_key="x") == {}


async def test_batch_search_tavily_mixed_failures(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """One query hits 401, another 429, another succeeds. None cascade."""
    httpx_mock.add_response(
        url=TAVILY_API_URL, method="POST", status_code=401
    )
    httpx_mock.add_response(
        url=TAVILY_API_URL, method="POST", status_code=429
    )
    httpx_mock.add_response(
        url=TAVILY_API_URL, method="POST", json=_SAMPLE_PAYLOAD
    )
    got = await batch_search_tavily(
        ["a", "b", "c"], api_key="key", concurrency=1
    )
    # Order across the three queries is not deterministic due to concurrency;
    # what we care about is that every query key is present, and exactly one
    # has results (the 200 response), while the other two are empty.
    assert set(got.keys()) == {"a", "b", "c"}
    non_empty = [k for k, v in got.items() if v]
    assert len(non_empty) == 1


async def test_batch_search_tavily_per_query_timeout_does_not_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a fake `search_tavily` that sleeps forever for one query;
    the batch-level `asyncio.wait_for` must drop it without touching others."""

    async def _fake_search(q: str, api_key: str, **_: Any) -> list[TavilyResult]:
        if q == "slow":
            await asyncio.sleep(5.0)
            return []
        return [
            TavilyResult(
                title="ok",
                url=f"https://example.com/{q}",
                content="c",
                score=0.5,
                query=q,
            )
        ]

    monkeypatch.setattr(
        "agent_worker.tools.tavily.search_tavily", _fake_search
    )

    got = await batch_search_tavily(
        ["fast", "slow"], api_key="k", concurrency=2, timeout_s=0.1
    )
    assert got["slow"] == []  # dropped by timeout
    assert len(got["fast"]) == 1
    assert got["fast"][0].url == "https://example.com/fast"


async def test_batch_search_tavily_concurrency_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With concurrency=1, the second search waits for the first to finish."""
    import time as _time

    in_flight = 0
    max_in_flight = 0

    async def _fake_search(q: str, api_key: str, **_: Any) -> list[TavilyResult]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return []

    monkeypatch.setattr(
        "agent_worker.tools.tavily.search_tavily", _fake_search
    )
    t0 = _time.monotonic()
    await batch_search_tavily(
        ["a", "b", "c"], api_key="k", concurrency=1
    )
    elapsed = _time.monotonic() - t0
    assert max_in_flight == 1
    # 3 searches × 0.02s each, serialized ≥ 0.05s (allow scheduler slack).
    assert elapsed >= 0.05


def test_tavily_result_is_frozen() -> None:
    r = TavilyResult(title="t", url="https://x", content="c", score=0.5)
    with pytest.raises(Exception):  # frozen dataclass → FrozenInstanceError
        r.score = 0.9  # type: ignore[misc]


# Unused import guard so orjson shows as used when running under strict ruff.
_ = orjson, httpx
