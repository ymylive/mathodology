"""HMMLService: load MethodNode seeds and retrieve via BM25 + dense-vector hybrid.

Design notes (M9 → M11):
- M9 shipped BM25-only retrieval: good for exact terminology and CJK characters
  (where subword embeddings often underperform), but blind to synonym drift
  (e.g. "traffic-flow optimization" vs seed "vehicle routing").
- M11 adds a dense-vector channel via `fastembed` (ONNX-only, CPU-only, no
  PyTorch). We keep BM25 alongside and fuse the two channels with min-max
  normalized weighted sum. In-process numpy cosine is trivial at 31 docs; no
  need for Qdrant.
- Embedding model: BAAI/bge-small-zh-v1.5 (512-dim, ~100 MB, bilingual CN+EN).
  Chosen because our seed corpus is bilingual (each method carries both
  English and Chinese keyword variants) and BGE is tuned for Chinese while
  still capable on English. It is smaller than the multilingual-e5 variants
  and the quality trade-off is acceptable at 31 docs.
- The vector index is computed once per process and cached on disk keyed by
  (model_name, num_methods, seed content hash) so seed edits invalidate the
  cache but a restart does not re-embed.
- If fastembed init fails (offline, ONNX runtime issue, download timeout) the
  service degrades to BM25-only with a warning — it MUST NOT crash the worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from mm_contracts import MethodNode
from rank_bm25 import BM25Okapi

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterable

_log = logging.getLogger(__name__)

# Split on anything that isn't an ASCII alnum or a CJK character in the BMP.
# CJK range 0x4e00-0x9fff covers the common unified ideographs used in modern
# Chinese problem statements.
_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9\u4e00-\u9fff]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")

_DEFAULT_SEED_DIR = Path(__file__).parent / "seed"
DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mathodology" / "hmml"


class _Embedder(Protocol):
    """Structural protocol matching the subset of TextEmbedding we rely on."""

    def embed(self, texts: Iterable[str], /) -> Any: ...


def _tokenize(text: str) -> list[str]:
    """Tokenize for BM25.

    - Split on non-word/non-CJK chars.
    - Lowercase ASCII words.
    - Further split runs of CJK characters into one token per character.
    """
    tokens: list[str] = []
    if not text:
        return tokens
    for raw in _TOKEN_SPLIT.split(text):
        if not raw:
            continue
        buf: list[str] = []
        for ch in raw:
            if _CJK_CHAR.match(ch):
                if buf:
                    tokens.append("".join(buf).lower())
                    buf = []
                tokens.append(ch)
            else:
                buf.append(ch)
        if buf:
            tokens.append("".join(buf).lower())
    return tokens


def _document_text(m: MethodNode) -> str:
    """Build the indexed text blob for one method (BM25 channel).

    Naturally weights name/scenarios/keywords via repetition vs. template code,
    which is long but usually has low overlap with the query.
    """
    parts: list[str] = [
        m.name,
        m.id.replace("_", " "),
        m.domain,
        m.subdomain,
        " ".join(m.applicable_scenarios),
        " ".join(m.keywords),
        " ".join(m.typical_cases),
        " ".join(m.common_pitfalls),
        m.math_form,
        m.python_template,
    ]
    return "\n".join(parts)


def _minmax_normalize(scores: np.ndarray) -> np.ndarray:
    """Min-max normalize to [0, 1]. All-equal input (incl. all-zero) → zeros."""
    lo = float(scores.min())
    hi = float(scores.max())
    if hi - lo < 1e-12:
        return np.zeros_like(scores, dtype=np.float64)
    return (scores.astype(np.float64) - lo) / (hi - lo)


def _seed_hash(methods: list[MethodNode]) -> str:
    """Stable 16-char hash of the method corpus. Changes invalidate the cache."""
    payload = [
        m.model_dump(mode="json")
        for m in sorted(methods, key=lambda x: x.id)
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _env_truthy(val: str | None) -> bool:
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "yes", "on"}


class HMMLService:
    """Load method seeds and answer BM25 + dense-vector hybrid retrieval queries."""

    def __init__(
        self,
        methods: list[MethodNode],
        *,
        embedder: _Embedder | None = None,
        cache_dir: Path | None = None,
        model_name: str = DEFAULT_EMBED_MODEL,
    ) -> None:
        self._methods: list[MethodNode] = methods
        self._tokenized_docs: list[list[str]] = [
            _tokenize(_document_text(m)) for m in methods
        ]
        self._index: BM25Okapi | None = self._build_bm25(self._tokenized_docs)
        self._embedder: _Embedder | None = embedder
        self._model_name: str = model_name
        self._vectors: np.ndarray | None = None  # shape (N, D), L2-normalized
        if self._embedder is not None and self._methods:
            try:
                self._vectors = self._load_or_build_vectors(cache_dir)
            except Exception as exc:  # noqa: BLE001 — degrade, don't crash
                _log.warning(
                    "HMML vector index build failed; degrading to BM25-only: %s",
                    exc,
                )
                self._vectors = None

    @classmethod
    def from_seed_dir(
        cls,
        seed_dir: Path | None = None,
        *,
        enable_vector: bool | None = None,
        cache_dir: Path | None = None,
        model_name: str = DEFAULT_EMBED_MODEL,
    ) -> HMMLService:
        """Load every `*.json` under `seed_dir/**` and validate as MethodNode.

        Lazily instantiates `fastembed.TextEmbedding` when `enable_vector` is
        truthy. `enable_vector=None` reads `HMML_VECTOR` from the env
        (1/true/yes/on → on, anything else → off). Default: ON.

        If fastembed init fails (network, ONNX runtime, disk), we log a
        warning and degrade to BM25-only — we do NOT raise.
        """
        path = seed_dir or _DEFAULT_SEED_DIR
        methods: list[MethodNode] = []
        if path.is_dir():
            for jf in sorted(path.rglob("*.json")):
                with jf.open("r", encoding="utf-8") as f:
                    data: Any = json.load(f)
                methods.append(MethodNode.model_validate(data))

        if enable_vector is None:
            # Default ON unless explicitly disabled via env.
            env_val = os.environ.get("HMML_VECTOR")
            enable_vector = True if env_val is None else _env_truthy(env_val)

        embedder: _Embedder | None = None
        if enable_vector and methods:
            try:
                # Imported lazily so that test environments without fastembed
                # (or offline CI that never enables vector) don't pay for it.
                from fastembed import TextEmbedding  # noqa: PLC0415

                embedder = TextEmbedding(model_name=model_name)
            except Exception as exc:  # noqa: BLE001 — degrade, don't crash
                _log.warning(
                    "fastembed init failed (%s=%s); HMML will run BM25-only: %s",
                    "model_name",
                    model_name,
                    exc,
                )
                embedder = None

        return cls(
            methods,
            embedder=embedder,
            cache_dir=cache_dir,
            model_name=model_name,
        )

    @property
    def methods(self) -> list[MethodNode]:
        """All loaded methods, in load order (sorted by path)."""
        return list(self._methods)

    @property
    def has_vector_index(self) -> bool:
        """Whether the vector channel is available for hybrid retrieval."""
        return self._vectors is not None

    def retrieve(
        self, query: str, top_k: int = 5
    ) -> list[tuple[MethodNode, float]]:
        """Return the top-k methods ranked by BM25 score, descending.

        Kept for backward compatibility / fallback. An empty query or empty
        index returns `[]` rather than raising.
        """
        if not self._methods or self._index is None:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._index.get_scores(tokens)
        ranked = sorted(
            zip(self._methods, scores, strict=True),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return [(m, float(s)) for m, s in ranked[:top_k]]

    def retrieve_hybrid(
        self,
        query: str,
        top_k: int = 5,
        *,
        bm25_weight: float = 0.4,
        vec_weight: float = 0.6,
    ) -> list[tuple[MethodNode, float]]:
        """BM25 + dense cosine hybrid retrieval.

        Each channel is min-max normalized to [0, 1] over the N candidates
        before the weighted sum. Degrades gracefully:
        - embedder not available → pure BM25
        - all BM25 scores == 0 (no token overlap) → pure vector
        - both channels collapse → empty
        """
        if not self._methods or self._index is None:
            return []
        if self._vectors is None:
            # No dense channel. Fall through to BM25-only.
            return self.retrieve(query, top_k=top_k)

        tokens = _tokenize(query)
        raw_bm25 = (
            self._index.get_scores(tokens) if tokens else np.zeros(len(self._methods))
        )
        bm25_scores = np.asarray(raw_bm25, dtype=np.float64)

        try:
            q_vec = self._embed_query(query)
            vec_scores = self._vectors @ q_vec
        except Exception as exc:  # noqa: BLE001 — degrade on embed failure
            _log.warning("query embedding failed; falling back to BM25: %s", exc)
            return self.retrieve(query, top_k=top_k)

        bm25_nz = float(bm25_scores.max()) > 0.0
        vec_nz = bool(np.any(np.abs(vec_scores) > 1e-9))

        if not bm25_nz and not vec_nz:
            return []
        if not bm25_nz:
            norm_vec = _minmax_normalize(vec_scores)
            fused = norm_vec
        elif not vec_nz:
            norm_bm25 = _minmax_normalize(bm25_scores)
            fused = norm_bm25
        else:
            norm_bm25 = _minmax_normalize(bm25_scores)
            norm_vec = _minmax_normalize(vec_scores)
            fused = bm25_weight * norm_bm25 + vec_weight * norm_vec

        ranked = sorted(
            zip(self._methods, fused, strict=True),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return [(m, float(s)) for m, s in ranked[:top_k]]

    # --- internals -----------------------------------------------------

    def _build_bm25(
        self, tokenized_docs: list[list[str]]
    ) -> BM25Okapi | None:
        if not tokenized_docs:
            return None
        if not any(tokenized_docs):
            return None
        return BM25Okapi(tokenized_docs)

    def _doc_text_for_embedding(self, m: MethodNode) -> str:
        """Text blob used for the dense embedding channel.

        We concatenate name, domain hints, applicable scenarios, typical cases,
        and keywords. We deliberately exclude `python_template` — code tokens
        (identifiers, imports) pollute the semantic signal — and `math_form`
        because LaTeX source tokens don't embed meaningfully.
        """
        parts: list[str] = [
            m.name,
            f"{m.domain} / {m.subdomain}",
            " ".join(m.applicable_scenarios),
            " ".join(m.typical_cases),
            " ".join(m.keywords),
        ]
        return "\n".join(p for p in parts if p)

    def _embed_query(self, query: str) -> np.ndarray:
        """Embed a single query string into a L2-normalized float64 vector."""
        assert self._embedder is not None  # guarded at call sites
        vec = next(iter(self._embedder.embed([query])))
        arr = np.asarray(vec, dtype=np.float64).reshape(-1)
        norm = np.linalg.norm(arr)
        if norm > 1e-12:
            arr = arr / norm
        return arr

    def _embed_docs(self) -> np.ndarray:
        """Embed every seed method. L2-normalize rows defensively."""
        assert self._embedder is not None  # guarded at call sites
        texts = [self._doc_text_for_embedding(m) for m in self._methods]
        vecs = list(self._embedder.embed(texts))
        mat = np.asarray(vecs, dtype=np.float64)
        if mat.ndim != 2:
            raise ValueError(f"unexpected embedding shape: {mat.shape}")
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms > 1e-12, norms, 1.0)
        return mat / norms

    def _load_or_build_vectors(self, cache_dir: Path | None) -> np.ndarray:
        """Load embeddings from disk if meta matches, else compute + persist."""
        cdir = cache_dir or _DEFAULT_CACHE_DIR
        meta_path = cdir / "meta.json"
        vec_path = cdir / "vectors.npy"
        expected_hash = _seed_hash(self._methods)

        if meta_path.is_file() and vec_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if (
                    meta.get("model_name") == self._model_name
                    and meta.get("num_methods") == len(self._methods)
                    and meta.get("seed_hashes") == expected_hash
                ):
                    arr = np.load(vec_path)
                    if arr.shape[0] == len(self._methods):
                        return arr.astype(np.float64, copy=False)
            except Exception as exc:  # noqa: BLE001 — cache is best-effort
                _log.warning("HMML vector cache unreadable, recomputing: %s", exc)

        # Cache miss or invalid: compute and persist.
        mat = self._embed_docs()
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            np.save(vec_path, mat)
            meta_path.write_text(
                json.dumps(
                    {
                        "model_name": self._model_name,
                        "num_methods": len(self._methods),
                        "seed_hashes": expected_hash,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            _log.warning("HMML vector cache write failed: %s", exc)
        return mat


__all__ = ["DEFAULT_EMBED_MODEL", "HMMLService"]
