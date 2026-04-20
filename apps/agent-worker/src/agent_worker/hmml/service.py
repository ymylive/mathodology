"""HMMLService: load MethodNode seeds from disk and retrieve via BM25.

Design notes:
- No vector embeddings, no LLM rerank — pure BM25 over a hand-tokenized text
  blob per method. This is "boring retrieval" over ~30 docs; correctness of the
  seed content matters far more than clever scoring.
- Tokenizer: split on non-word chars, lowercase ASCII, CJK chars become
  individual tokens (character-level). Good enough for a 30-doc corpus.
- Loader is fault-tolerant: a missing / empty seed dir yields an empty service
  that returns no results but doesn't raise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mm_contracts import MethodNode
from rank_bm25 import BM25Okapi

# Split on anything that isn't an ASCII alnum or a CJK character in the BMP.
# CJK range 0x4e00-0x9fff covers the common unified ideographs used in modern
# Chinese problem statements.
_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9\u4e00-\u9fff]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")

_DEFAULT_SEED_DIR = Path(__file__).parent / "seed"


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
        # A chunk may be pure ASCII, pure CJK, or a mixture (e.g. "M/M/c" after
        # split is "M", but "数据k均值" arrives as one chunk). We iterate runs.
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
    """Build the indexed text blob for one method.

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


class HMMLService:
    """Load method seeds and answer BM25 retrieval queries."""

    def __init__(self, methods: list[MethodNode]) -> None:
        self._methods: list[MethodNode] = methods
        self._tokenized_docs: list[list[str]] = [
            _tokenize(_document_text(m)) for m in methods
        ]
        self._index: BM25Okapi | None = self._build_bm25(self._tokenized_docs)

    @classmethod
    def from_seed_dir(cls, seed_dir: Path | None = None) -> HMMLService:
        """Load every `*.json` under `seed_dir/**` and validate as MethodNode.

        Files that fail schema validation or that aren't valid JSON are skipped
        with a ValueError (so the caller can notice a corrupted seed), but a
        completely missing / empty directory yields an empty service.
        """
        path = seed_dir or _DEFAULT_SEED_DIR
        methods: list[MethodNode] = []
        if path.is_dir():
            for jf in sorted(path.rglob("*.json")):
                with jf.open("r", encoding="utf-8") as f:
                    data: Any = json.load(f)
                methods.append(MethodNode.model_validate(data))
        return cls(methods)

    @property
    def methods(self) -> list[MethodNode]:
        """All loaded methods, in load order (sorted by path)."""
        return list(self._methods)

    def retrieve(
        self, query: str, top_k: int = 5
    ) -> list[tuple[MethodNode, float]]:
        """Return the top-k methods ranked by BM25 score, descending.

        An empty query or empty index returns `[]` rather than raising.
        """
        if not self._methods or self._index is None:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._index.get_scores(tokens)
        # argsort desc without numpy dependency on method-count order.
        ranked = sorted(
            zip(self._methods, scores, strict=True),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return [(m, float(s)) for m, s in ranked[:top_k]]

    def _build_bm25(
        self, tokenized_docs: list[list[str]]
    ) -> BM25Okapi | None:
        if not tokenized_docs:
            return None
        # BM25Okapi needs at least one non-empty doc. Our seeds all carry
        # content, but guard anyway so a malformed seed doesn't crash startup.
        if not any(tokenized_docs):
            return None
        return BM25Okapi(tokenized_docs)


__all__ = ["HMMLService"]
