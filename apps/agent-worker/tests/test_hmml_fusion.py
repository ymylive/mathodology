"""Tests for M11: BM25 + dense vector hybrid retrieval.

We mock fastembed with a deterministic hashed-bag-of-words embedder so tests
never touch the network and never download the ~100 MB ONNX model. A real
fastembed integration test would live behind `@pytest.mark.slow`; we omit it
here to keep `pytest` fast + offline-safe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np
import pytest
from agent_worker.hmml import HMMLService
from agent_worker.hmml.service import _tokenize


class _HashedBowEmbedder:
    """Deterministic stub embedder.

    Tokenizes each text with the same tokenizer HMMLService uses for BM25,
    then accumulates hashed-bucket counts into a fixed-dim vector. Semantic
    similarity is approximated by token overlap — good enough to exercise the
    fusion path in tests, terrible as an actual embedding.
    """

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim
        # Pre-expand a small synonym map so tests can exercise synonym drift
        # without needing a real model. Queries containing one side get extra
        # tokens from the other side added to their bag.
        self._synonyms = {
            "traveling": ["traveling", "salesman", "routing", "vehicle"],
            "salesman": ["traveling", "salesman", "routing", "vehicle"],
            "tsp": ["traveling", "salesman", "routing", "vehicle"],
            "vehicle": ["vehicle", "routing", "traveling", "salesman"],
            "routing": ["routing", "vehicle", "traveling", "salesman"],
            "traffic": ["traffic", "routing", "vehicle", "flow"],
            "flow": ["flow", "traffic", "routing"],
            "optimization": ["optimization", "optimize"],
        }

    def _bucket(self, tok: str) -> int:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "big") % self.dim

    def _vectorize(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float64)
        toks = _tokenize(text)
        expanded: list[str] = []
        for t in toks:
            expanded.extend(self._synonyms.get(t, [t]))
        for t in expanded:
            v[self._bucket(t)] += 1.0
        # fastembed returns normalized vectors — emulate that here.
        norm = np.linalg.norm(v)
        if norm > 1e-12:
            v = v / norm
        return v

    def embed(self, texts: Iterable[str]) -> Iterator[np.ndarray]:
        for t in texts:
            yield self._vectorize(t)


def _safe_cache_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def stub_embedder() -> _HashedBowEmbedder:
    return _HashedBowEmbedder(dim=256)


@pytest.fixture
def hybrid_service(
    stub_embedder: _HashedBowEmbedder, tmp_path: Path
) -> HMMLService:
    """Real seed corpus + stub embedder + isolated cache dir.

    Uses `from_seed_dir(enable_vector=False)` just to load methods from disk,
    then rebuilds the service around the stub embedder we actually want.
    """
    loaded = HMMLService.from_seed_dir(enable_vector=False)
    return HMMLService(
        methods=loaded.methods,
        embedder=stub_embedder,
        cache_dir=_safe_cache_dir(tmp_path, "hmml-cache"),
        model_name="stub-bow-v1",
    )


def test_hybrid_has_vector_index(hybrid_service: HMMLService) -> None:
    assert hybrid_service.has_vector_index
    assert len(hybrid_service.methods) >= 30


def test_hybrid_shape_matches_bm25(hybrid_service: HMMLService) -> None:
    """retrieve_hybrid returns same (MethodNode, float) shape and respects top_k."""
    results = hybrid_service.retrieve_hybrid("linear regression", top_k=3)
    assert len(results) == 3
    for m, score in results:
        assert hasattr(m, "id")
        assert isinstance(score, float)
    # Scores must be descending.
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_synonym_drift_pure_english(hybrid_service: HMMLService) -> None:
    """`traveling salesman optimization` has no BM25 overlap with seed names
    but the vector channel (via synonym expansion) should surface a method
    whose typical cases include `traveling salesman and vehicle routing` —
    the genetic_algorithm seed. This exercises the pure-synonym-drift path.
    """
    results = hybrid_service.retrieve_hybrid(
        "traveling salesman optimization", top_k=5
    )
    ids = [m.id for m, _ in results]
    assert "genetic_algorithm" in ids, (
        f"genetic_algorithm not in top-5 for TSP synonym query: {ids}"
    )


def test_hybrid_cjk_shortest_path_still_surfaces_dijkstra(
    hybrid_service: HMMLService,
) -> None:
    """CJK query still pinned by BM25 channel (stub embedder doesn't know CJK
    synonyms). Dijkstra must stay top-3."""
    results = hybrid_service.retrieve_hybrid("最短路径", top_k=3)
    ids = [m.id for m, _ in results]
    assert "dijkstra" in ids, f"dijkstra not in top-3 for CJK query: {ids}"


def test_hybrid_mixed_query_returns_results(hybrid_service: HMMLService) -> None:
    """Mixed EN+CN query must not crash and must return non-empty results."""
    results = hybrid_service.retrieve_hybrid("聚类 clustering", top_k=3)
    assert results, "expected at least one result for mixed-language query"


def test_hybrid_empty_query_returns_empty(hybrid_service: HMMLService) -> None:
    # Empty query → no BM25 tokens AND vector score collapses (no tokens → zero
    # vector → zero cosine). Both channels degenerate → []. This is the
    # documented behavior: better to return nothing than noise.
    assert hybrid_service.retrieve_hybrid("", top_k=5) == []


def test_hybrid_bm25_zero_falls_through_to_vector(
    hybrid_service: HMMLService,
) -> None:
    """If BM25 finds nothing (tokens don't overlap any doc), vector channel
    alone decides the ranking — and we still get a non-empty list."""
    # 'traffic' is not a keyword in any seed; BM25 score should be 0 for most
    # docs. The synonym map maps it to routing/vehicle, so vector surfaces GA.
    results = hybrid_service.retrieve_hybrid(
        "traffic flow vehicle routing", top_k=3
    )
    assert results, "expected non-empty results from vector fallback"
    ids = [m.id for m, _ in results]
    assert "genetic_algorithm" in ids


def test_hybrid_weights_affect_ranking(hybrid_service: HMMLService) -> None:
    """Shifting weight from BM25 to vector should (at least sometimes) reorder
    results. Not a strict ordering test — just assert the two weightings don't
    always produce identical ranked ID lists across a handful of queries."""
    queries = [
        "linear regression",
        "genetic algorithm traveling salesman",
        "clustering unsupervised",
    ]
    any_different = False
    for q in queries:
        r_bm25_heavy = hybrid_service.retrieve_hybrid(
            q, top_k=5, bm25_weight=0.95, vec_weight=0.05
        )
        r_vec_heavy = hybrid_service.retrieve_hybrid(
            q, top_k=5, bm25_weight=0.05, vec_weight=0.95
        )
        if [m.id for m, _ in r_bm25_heavy] != [m.id for m, _ in r_vec_heavy]:
            any_different = True
            break
    assert any_different, "weight changes had no observable effect on ranking"


def test_degraded_path_hybrid_equals_bm25(hybrid_service: HMMLService) -> None:
    """When embedder is None, `retrieve_hybrid` returns the same ranking as
    `retrieve`. Uses the hybrid_service's method list to make a paired svc."""
    bare = HMMLService(methods=hybrid_service.methods, embedder=None)
    assert not bare.has_vector_index
    q = "time series forecasting seasonality"
    a = bare.retrieve(q, top_k=5)
    b = bare.retrieve_hybrid(q, top_k=5)
    assert [m.id for m, _ in a] == [m.id for m, _ in b]
    assert [round(s, 6) for _, s in a] == [round(s, 6) for _, s in b]


def test_cache_roundtrip_byte_identical(
    stub_embedder: _HashedBowEmbedder, tmp_path: Path
) -> None:
    """First build writes vectors.npy + meta.json; second build reads them
    back without re-embedding. Vectors must be byte-identical."""
    cache_dir = _safe_cache_dir(tmp_path, "roundtrip")
    methods = HMMLService.from_seed_dir(enable_vector=False).methods

    svc1 = HMMLService(
        methods=methods,
        embedder=stub_embedder,
        cache_dir=cache_dir,
        model_name="stub-bow-v1",
    )
    assert svc1.has_vector_index
    v1 = svc1._vectors  # noqa: SLF001 — test internals on purpose
    assert v1 is not None
    assert (cache_dir / "vectors.npy").is_file()
    assert (cache_dir / "meta.json").is_file()

    # Build a second service pointing at the same cache. This one should read
    # from disk; to prove that, use a different stub instance AND swap in one
    # whose embed() raises — a cache hit means embed() is never called.
    class _ExplodingEmbedder:
        def embed(self, texts: Iterable[str]) -> Iterator[np.ndarray]:
            raise AssertionError(
                "embed() should not be called on cache hit"
            )
            yield  # pragma: no cover — unreachable, keeps generator typing

    svc2 = HMMLService(
        methods=methods,
        embedder=_ExplodingEmbedder(),
        cache_dir=cache_dir,
        model_name="stub-bow-v1",
    )
    v2 = svc2._vectors  # noqa: SLF001
    assert v2 is not None
    assert v1.shape == v2.shape
    assert v1.shape[0] == len(methods)
    np.testing.assert_array_equal(v1, v2)


def test_cache_invalidates_on_model_name_change(
    stub_embedder: _HashedBowEmbedder, tmp_path: Path
) -> None:
    cache_dir = _safe_cache_dir(tmp_path, "invalidate")
    methods = HMMLService.from_seed_dir(enable_vector=False).methods

    HMMLService(
        methods=methods,
        embedder=stub_embedder,
        cache_dir=cache_dir,
        model_name="stub-bow-v1",
    )
    # Different model_name → cache must miss and re-embed.
    svc2 = HMMLService(
        methods=methods,
        embedder=stub_embedder,
        cache_dir=cache_dir,
        model_name="stub-bow-v2",
    )
    import json as _json  # noqa: PLC0415

    meta = _json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["model_name"] == "stub-bow-v2"
    assert svc2.has_vector_index


def test_from_seed_dir_env_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HMML_VECTOR=0` must result in no vector index (BM25 only)."""
    monkeypatch.setenv("HMML_VECTOR", "0")
    svc = HMMLService.from_seed_dir()
    assert not svc.has_vector_index
    # BM25 still works.
    assert svc.retrieve("最短路径", top_k=1)


def test_from_seed_dir_fastembed_failure_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate fastembed init failure; service must load BM25-only, no crash."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "fastembed":
            raise RuntimeError("simulated: no network / ONNX broken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("HMML_VECTOR", "1")

    svc = HMMLService.from_seed_dir()
    assert not svc.has_vector_index
    # BM25 still works.
    results = svc.retrieve("最短路径", top_k=1)
    assert results
