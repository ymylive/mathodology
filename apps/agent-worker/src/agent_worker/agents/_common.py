"""Helpers shared across agent modules."""

from __future__ import annotations

import re

_PROBLEM_LETTER_RE = re.compile(r"Problem\s+([A-F])\b", re.IGNORECASE)


def problem_letter_from_problem_text(problem_text: str) -> str | None:
    """Best-effort: detect the MCM/ICM problem letter from the prompt header.

    Returns the uppercase letter (A-F) or None when the header doesn't have a
    'Problem X' marker (e.g. CUMCM problems don't use letters).
    """
    if not problem_text:
        return None
    m = _PROBLEM_LETTER_RE.search(problem_text)
    return m.group(1).upper() if m else None
