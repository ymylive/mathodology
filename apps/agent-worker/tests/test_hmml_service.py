"""Tests for the HMML (Hierarchical Math Modeling Library) service.

Validates that the on-disk seed corpus loads cleanly, BM25 retrieval surfaces
the expected canonical method for each domain, and CJK tokenization works.
"""

from __future__ import annotations

import pytest
from agent_worker.hmml import HMMLService


@pytest.fixture(scope="module")
def service() -> HMMLService:
    # Keep the BM25-only test suite offline/network-free and fast: the M11
    # vector channel is covered in `test_hmml_fusion.py` with a stub embedder.
    return HMMLService.from_seed_dir(enable_vector=False)


def test_seed_dir_loads_at_least_30_methods(service: HMMLService) -> None:
    assert len(service.methods) >= 30


def test_retrieve_ols_for_linear_regression(service: HMMLService) -> None:
    results = service.retrieve(
        "linear regression fit with ordinary least squares", top_k=3
    )
    assert results, "expected at least one result"
    top = results[0][0]
    assert top.id in {"ols_linear_regression", "ridge_regression", "lasso_regression"}
    # Specifically, OLS should be rank-1 for the exact textbook phrasing.
    assert top.id == "ols_linear_regression"


def test_retrieve_time_series_returns_seasonal_methods(service: HMMLService) -> None:
    results = service.retrieve("time series forecasting seasonality", top_k=5)
    ids = {m.id for m, _ in results}
    assert ids & {"arima", "stl_decomposition", "exponential_smoothing"}, (
        f"expected at least one of ARIMA/STL/ExpSmoothing in top-5, got {ids}"
    )


def test_retrieve_cjk_shortest_path(service: HMMLService) -> None:
    """CJK query: Dijkstra should surface in the top-3 for `最短路径`."""
    results = service.retrieve("最短路径", top_k=3)
    ids = [m.id for m, _ in results]
    assert "dijkstra" in ids, f"dijkstra not in top-3 for CJK query: {ids}"


def test_retrieve_cjk_k_means(service: HMMLService) -> None:
    """Another CJK sanity check: K均值 should retrieve k-means."""
    results = service.retrieve("聚类 K均值", top_k=3)
    ids = [m.id for m, _ in results]
    assert "kmeans" in ids, f"kmeans not in top-3 for CJK clustering query: {ids}"


def test_retrieve_queueing_returns_mmc(service: HMMLService) -> None:
    results = service.retrieve("call center queueing multiple servers", top_k=5)
    ids = {m.id for m, _ in results}
    assert "mmc_queue" in ids, f"expected mmc_queue in top-5, got {ids}"


def test_retrieve_empty_query_does_not_crash(service: HMMLService) -> None:
    assert service.retrieve("", top_k=5) == []
    assert service.retrieve("   ", top_k=5) == []


def test_retrieve_top_k_is_capped(service: HMMLService) -> None:
    results = service.retrieve("optimization", top_k=3)
    assert len(results) <= 3


def test_scores_are_monotonically_non_increasing(service: HMMLService) -> None:
    results = service.retrieve("genetic algorithm metaheuristic", top_k=5)
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)


def test_empty_service_returns_empty_results() -> None:
    empty = HMMLService(methods=[])
    assert empty.retrieve("anything", top_k=5) == []


def test_retrieve_hybrid_degrades_to_bm25_without_embedder() -> None:
    """Without a vector index (embedder=None), hybrid MUST equal BM25."""
    bm25_only = HMMLService.from_seed_dir(enable_vector=False)
    assert not bm25_only.has_vector_index
    q = "linear regression fit with ordinary least squares"
    bm25_out = bm25_only.retrieve(q, top_k=3)
    hybrid_out = bm25_only.retrieve_hybrid(q, top_k=3)
    assert [m.id for m, _ in bm25_out] == [m.id for m, _ in hybrid_out]
    assert [round(s, 6) for _, s in bm25_out] == [
        round(s, 6) for _, s in hybrid_out
    ]
