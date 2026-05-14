"""Few-shot exemplar library for Writer/Modeler prompts.

Loads structured snippets from MCM/ICM winning papers (built offline from
the dick20 corpus, see `data/mcm/scripts/build_index.py`) and offers a
small surface to fetch the top-K exemplars matching a competition_type +
problem_letter and format them as a prompt block.

Degrades silently when the index file is missing — agents fall back to
their existing behaviour, no exemplars injected.
"""

from functools import lru_cache

from agent_worker.few_shot.loader import (
    DEFAULT_INDEX_PATH,
    Exemplar,
    FewShotLibrary,
    format_writer_block,
)


@lru_cache(maxsize=1)
def get_default_library() -> FewShotLibrary:
    """Process-wide singleton, loaded lazily. Reset with `.cache_clear()` in tests."""
    return FewShotLibrary.from_jsonl()


__all__ = [
    "DEFAULT_INDEX_PATH",
    "Exemplar",
    "FewShotLibrary",
    "format_writer_block",
    "get_default_library",
]
